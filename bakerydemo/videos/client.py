"""A thin, synchronous VideoGen API client.

This mirrors the REST contract documented by the VideoGen ``api`` skill:

* ``POST   /v1/workflows/script-to-video``          start a narrated video
* ``GET    /v1/workflows/runs/{workflowRunId}``      poll the workflow run
* ``POST   /v1/projects/{projectId}/export``         start an MP4 export
* ``GET    /v1/projects/{projectId}/exports/{id}``   poll the export
* (signed ``downloadUrl``)                            fetch the finished MP4

The client is deliberately small and synchronous: it runs inside a background
worker thread (see :mod:`bakerydemo.videos.services`), so there is no benefit to
async here and a plain ``requests.Session`` keeps behaviour easy to reason about.

Every provider failure is raised as a typed exception from
:mod:`bakerydemo.videos.exceptions` — callers never see a raw HTTP status code.

Cost control (see the mandate in the task): the request bodies this client sends
are the minimal "cheap shape". ``script-to-video`` is called with the narration
``script`` plus the configured ``visualStyle`` (which VideoGen requires) and
nothing else — no avatar/presenter, no image-to-video remix, and the documented
16:9 default; ``export`` is called exactly once with default (non-4K) quality.
The ``visualStyle`` value is supplied via settings (``VIDEOGEN_VISUAL_STYLE``),
never hard-coded, so it can be pointed at a different account without code
changes and so no AI-imagery value is ever baked in (see docs/videogen.md).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO

import requests
from django.conf import settings

from .exceptions import (
    VideoGenAPIError,
    VideoGenAuthError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenExportError,
    VideoGenRateLimitError,
    VideoGenResponseError,
    VideoGenTimeoutError,
    VideoGenWorkflowError,
)

DEFAULT_BASE_URL = "https://api.videogen.io"

#: Terminal workflow/export/tool statuses used across the VideoGen API.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

ProgressCallback = Callable[[float], None]


class VideoGenClient:
    """Synchronous client for the subset of the VideoGen API we use."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        http_timeout: float | None = None,
        poll_interval: float | None = None,
        session: requests.Session | None = None,
    ) -> None:
        resolved_key = api_key if api_key is not None else getattr(settings, "VIDEOGEN_API_KEY", "")
        if not resolved_key:
            raise VideoGenConfigurationError(
                "VideoGen API key is not configured. Set the VIDEOGEN_API_KEY "
                "environment variable."
            )
        self._api_key = resolved_key

        configured_base = (
            base_url
            if base_url is not None
            else getattr(settings, "VIDEOGEN_BASE_URL", None)
        )
        self.base_url = (configured_base or DEFAULT_BASE_URL).rstrip("/")

        self.http_timeout = (
            http_timeout
            if http_timeout is not None
            else float(getattr(settings, "VIDEOGEN_HTTP_TIMEOUT_SECONDS", 60))
        )
        self.poll_interval = (
            poll_interval
            if poll_interval is not None
            else float(getattr(settings, "VIDEOGEN_POLL_INTERVAL_SECONDS", 5))
        )

        self._owns_session = session is None
        self._session = session or requests.Session()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> VideoGenClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low-level ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "X-VideoGen-Client": "bakerydemo-videos",
        }
        try:
            response = self._session.request(
                method=method,
                url=url,
                json=json,
                headers=headers,
                timeout=self.http_timeout,
            )
        except requests.Timeout as exc:
            raise VideoGenTimeoutError(
                f"VideoGen request timed out: {method} {path}"
            ) from exc
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Could not reach VideoGen: {method} {path} ({exc})"
            ) from exc

        request_id = response.headers.get("x-request-id")

        if response.status_code >= 400:
            self._raise_for_status(response, request_id)

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise VideoGenResponseError(
                f"VideoGen returned a non-JSON response for {method} {path}",
                status=response.status_code,
                request_id=request_id,
                body=response.text[:500],
            ) from exc

    @staticmethod
    def _raise_for_status(response: requests.Response, request_id: str | None) -> None:
        status = response.status_code
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text[:500]

        message = f"VideoGen API request failed with status {status}"
        code = None
        if isinstance(body, Mapping):
            message = body.get("message") or body.get("error") or message
            code = body.get("code")
            if not isinstance(message, str):
                message = f"VideoGen API request failed with status {status}"

        kwargs = {
            "status": status,
            "code": code,
            "request_id": request_id,
            "body": body,
        }
        if status in (401, 403):
            raise VideoGenAuthError(message, **kwargs)
        if status == 429:
            raise VideoGenRateLimitError(message, **kwargs)
        raise VideoGenAPIError(message, **kwargs)

    # -- account -----------------------------------------------------------

    def get_me(self) -> dict:
        """Return the account/team behind the API key. Useful as a free
        connection test (``GET /v1/me``) before producing a video."""
        data = self._request("GET", "/v1/me")
        if not isinstance(data, Mapping):
            raise VideoGenResponseError(
                "VideoGen returned an unexpected /v1/me payload.", body=data
            )
        return dict(data)

    # -- workflows ---------------------------------------------------------

    def create_script_to_video(
        self,
        script: str,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> dict:
        """Start a script-to-video workflow. Returns the started run.

        Response shape: ``{"workflowRunId", "projectId", "projectUrl"}``.
        """
        body: dict[str, Any] = {"script": script}
        if extra:
            body.update(extra)
        data = self._request("POST", "/v1/workflows/script-to-video", json=body)
        if not isinstance(data, Mapping) or not data.get("workflowRunId"):
            raise VideoGenResponseError(
                "VideoGen did not return a workflowRunId for script-to-video.",
                body=data,
            )
        return dict(data)

    def get_workflow_run(self, workflow_run_id: str) -> dict:
        data = self._request("GET", f"/v1/workflows/runs/{workflow_run_id}")
        if not isinstance(data, Mapping):
            raise VideoGenResponseError(
                "VideoGen returned an unexpected workflow run payload.", body=data
            )
        return dict(data)

    # -- projects / exports ------------------------------------------------

    def export_project(
        self,
        project_id: str,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> dict:
        """Start a single MP4 export of a project. Returns ``{"exportId"}``."""
        body: dict[str, Any] = {}
        if extra:
            body.update(extra)
        data = self._request(
            "POST", f"/v1/projects/{project_id}/export", json=body or None
        )
        if not isinstance(data, Mapping) or not data.get("exportId"):
            raise VideoGenResponseError(
                "VideoGen did not return an exportId for the project export.",
                body=data,
            )
        return dict(data)

    def get_project_export(self, project_id: str, export_id: str) -> dict:
        data = self._request(
            "GET", f"/v1/projects/{project_id}/exports/{export_id}"
        )
        if not isinstance(data, Mapping):
            raise VideoGenResponseError(
                "VideoGen returned an unexpected export payload.", body=data
            )
        return dict(data)

    def download_to(self, download_url: str, target: BinaryIO) -> int:
        """Stream a finished export (signed ``downloadUrl``) into ``target``.

        Returns the number of bytes written. The download URL is pre-signed, so
        it is fetched without the API bearer credentials.
        """
        try:
            with requests.get(
                download_url, stream=True, timeout=self.http_timeout
            ) as response:
                if response.status_code >= 400:
                    raise VideoGenAPIError(
                        "Failed to download the finished MP4 from VideoGen.",
                        status=response.status_code,
                    )
                written = 0
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        target.write(chunk)
                        written += len(chunk)
                return written
        except requests.Timeout as exc:
            raise VideoGenTimeoutError("Timed out downloading the MP4.") from exc
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Could not download the MP4: {exc}"
            ) from exc

    # -- polling helpers ---------------------------------------------------

    def wait_for_workflow(
        self,
        workflow_run_id: str,
        *,
        on_progress: ProgressCallback | None = None,
        deadline: float | None = None,
    ) -> dict:
        """Poll a workflow run until it succeeds; raise on failure/timeout."""
        return self._poll(
            fetch=lambda: self.get_workflow_run(workflow_run_id),
            on_progress=on_progress,
            deadline=deadline,
            failure_exc=VideoGenWorkflowError,
            kind="workflow run",
        )

    def wait_for_export(
        self,
        project_id: str,
        export_id: str,
        *,
        on_progress: ProgressCallback | None = None,
        deadline: float | None = None,
    ) -> dict:
        """Poll a project export until it succeeds; raise on failure/timeout."""
        return self._poll(
            fetch=lambda: self.get_project_export(project_id, export_id),
            on_progress=on_progress,
            deadline=deadline,
            failure_exc=VideoGenExportError,
            kind="project export",
        )

    def _poll(
        self,
        *,
        fetch: Callable[[], dict],
        on_progress: ProgressCallback | None,
        deadline: float | None,
        failure_exc: type,
        kind: str,
    ) -> dict:
        while True:
            payload = fetch()
            status = payload.get("status")

            progress = payload.get("progressPercentage")
            if on_progress is not None and isinstance(progress, (int, float)):
                on_progress(float(progress))

            if status in TERMINAL_STATUSES:
                if status == "succeeded":
                    return payload
                raise failure_exc(
                    self._terminal_error_message(payload, kind, status),
                    status=None,
                    body=payload,
                )

            if deadline is not None and time.monotonic() >= deadline:
                raise VideoGenTimeoutError(
                    f"Timed out waiting for the VideoGen {kind} to finish.",
                    body=payload,
                )
            time.sleep(self.poll_interval)

    @staticmethod
    def _terminal_error_message(payload: Mapping[str, Any], kind: str, status: str) -> str:
        error = payload.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        if isinstance(error, str) and error:
            return error
        return f"VideoGen {kind} {status}."
