"""A thin, typed HTTP client for the VideoGen API.

Only the handful of endpoints this integration needs are implemented, and each
one maps a VideoGen REST call onto a small Python method that returns the parsed
JSON body (or, for the final MP4, raw bytes). Any failure — a network problem, a
non-2xx response, a workflow/export that ends in ``failed``/``cancelled``, or a
poll that never reaches a terminal state — is raised as a typed exception from
:mod:`bakerydemo.videogen.exceptions`.

The endpoints, request shapes and response shapes used here come solely from the
VideoGen "api" skill and its referenced OpenAPI specification. The request is
deliberately the cheapest shape the skill documents: ``script-to-video`` with
``STOCK`` footage (never AI imagery), a plain narration voice (no actor/avatar),
a single 16:9 project, and a single ``STANDARD`` (lowest tier, never 4K) export.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import requests
from django.conf import settings

from .exceptions import (
    VideoGenAPIError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenExportError,
    VideoGenTimeoutError,
    VideoGenWorkflowError,
)

#: Default VideoGen API base address (per the api skill). Overridable at runtime
#: with the ``VIDEOGEN_BASE_URL`` setting/env var.
DEFAULT_BASE_URL = "https://api.videogen.io"

#: Terminal workflow/export statuses (VideoGen ``JobStatus``).
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class VideoGenClient:
    """Client for the subset of the VideoGen API used to make one video."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        poll_interval: float = 3.0,
        poll_timeout: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        api_key = api_key if api_key is not None else settings.VIDEOGEN_API_KEY
        if not api_key:
            raise VideoGenConfigurationError(
                "VIDEOGEN_API_KEY is not set; cannot talk to VideoGen."
            )
        self._api_key = api_key

        configured_base = (
            base_url if base_url is not None else settings.VIDEOGEN_BASE_URL
        )
        # An empty/unset override falls back to the documented default.
        self.base_url = (configured_base or DEFAULT_BASE_URL).rstrip("/")

        self._session = session or requests.Session()
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._sleep = sleep
        # Monotonic clock; injectable in tests for deterministic timeouts.
        self._clock = clock

    # -- low level ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200, 202),
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method,
                url,
                json=json,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Network error calling VideoGen {method} {path}: {exc}"
            ) from exc

        if response.status_code not in expected:
            raise self._api_error(response, method, path)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise VideoGenAPIError(
                f"VideoGen {method} {path} returned a non-JSON body.",
                status_code=response.status_code,
                payload=response.text,
            ) from exc

    @staticmethod
    def _api_error(
        response: requests.Response, method: str, path: str
    ) -> VideoGenAPIError:
        code = None
        detail = None
        payload: object
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if isinstance(payload, dict):
            # VideoGen error bodies follow the ``ApiError`` shape, sometimes
            # nested under an "error" key.
            err = (
                payload.get("error")
                if isinstance(payload.get("error"), dict)
                else payload
            )
            code = err.get("code")
            detail = err.get("message") or payload.get("message")
        message = f"VideoGen {method} {path} failed with HTTP {response.status_code}"
        if detail:
            message = f"{message}: {detail}"
        return VideoGenAPIError(
            message,
            status_code=response.status_code,
            code=code,
            payload=payload,
        )

    # -- workflows ---------------------------------------------------------

    def create_script_to_video(
        self,
        *,
        script: str,
        aspect_ratio: tuple[int, int] = (16, 9),
    ) -> dict[str, Any]:
        """Start a ``script-to-video`` run for ``script`` (narrated verbatim).

        Cheap-shape request: STOCK footage only, no actor/avatar (voice-only
        narration), no remix actions (no image-to-video), 16:9. Returns the
        ``StartWorkflowRunResponse`` body (``workflowRunId``, ``projectId`` ...).
        """
        width, height = aspect_ratio
        body = {
            "script": script,
            "visualStyle": {"type": "STOCK"},
            "aspectRatio": {"width": width, "height": height},
        }
        return self._request(
            "POST", "/v1/workflows/script-to-video", json=body, expected=(202,)
        )

    def get_workflow_run(self, workflow_run_id: str) -> dict[str, Any]:
        return self._request(
            "GET", f"/v1/workflows/runs/{workflow_run_id}", expected=(200,)
        )

    def poll_workflow_run(
        self,
        workflow_run_id: str,
        *,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Poll a workflow run until it succeeds; raise on failure/timeout."""
        run = self._poll(
            lambda: self.get_workflow_run(workflow_run_id),
            what=f"workflow run {workflow_run_id}",
            on_progress=on_progress,
        )
        if run.get("status") != "succeeded":
            raise VideoGenWorkflowError(
                self._terminal_message("Workflow run", run),
                code=self._error_code(run),
            )
        return run

    # -- projects / exports ------------------------------------------------

    def export_project(
        self, project_id: str, *, quality: str = "STANDARD"
    ) -> dict[str, Any]:
        """Start a single MP4 export.

        ``quality="STANDARD"`` is the lowest ``ExportProjectQuality`` tier —
        the cheapest, and by definition never the 4K ``ULTRA_HIGH`` tier.
        """
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/export",
            json={"quality": quality},
            expected=(202,),
        )

    def get_project_export(self, project_id: str, export_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/projects/{project_id}/exports/{export_id}",
            expected=(200,),
        )

    def poll_project_export(
        self,
        project_id: str,
        export_id: str,
        *,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Poll an export until it succeeds; raise on failure/timeout."""
        export = self._poll(
            lambda: self.get_project_export(project_id, export_id),
            what=f"export {export_id}",
            on_progress=on_progress,
        )
        if export.get("status") != "succeeded":
            raise VideoGenExportError(
                self._terminal_message("Export", export),
                code=self._error_code(export),
            )
        return export

    # -- files -------------------------------------------------------------

    def download(self, url: str) -> bytes:
        """Download raw bytes from a (pre-signed) VideoGen URL."""
        try:
            response = self._session.get(url, timeout=self._timeout, stream=True)
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Network error downloading VideoGen asset: {exc}"
            ) from exc
        if response.status_code != 200:
            raise VideoGenAPIError(
                f"Downloading VideoGen asset failed with HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        return response.content

    # -- polling helper ----------------------------------------------------

    def _poll(
        self,
        fetch: Callable[[], dict[str, Any]],
        *,
        what: str,
        on_progress: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        started = self._clock()
        while True:
            state = fetch()
            if on_progress is not None:
                on_progress(state)
            status = state.get("status")
            if status in _TERMINAL_STATUSES:
                return state
            if self._clock() - started > self._poll_timeout:
                raise VideoGenTimeoutError(
                    f"Timed out after {self._poll_timeout:.0f}s waiting for "
                    f"{what} (last status: {status!r})."
                )
            self._sleep(self._poll_interval)

    @staticmethod
    def _error_code(state: dict[str, Any]) -> str | None:
        error = state.get("error")
        if isinstance(error, dict):
            return error.get("code")
        return None

    @staticmethod
    def _terminal_message(label: str, state: dict[str, Any]) -> str:
        status = state.get("status")
        error = state.get("error")
        detail = ""
        if isinstance(error, dict) and error.get("message"):
            detail = f": {error['message']}"
        return f"{label} ended with status {status!r}{detail}"
