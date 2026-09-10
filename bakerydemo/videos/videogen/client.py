"""HTTP client for the VideoGen API.

Scope is deliberately narrow: only the endpoints this integration needs to turn
a script into a narrated video, export it once, and fetch the finished MP4.
All endpoint paths, response field names, and status vocabulary follow the
VideoGen ``api`` skill (the sole reference for talking to VideoGen).

Design notes
------------
* The API key and base URL come from Django settings (which read them from the
  environment). Nothing is hard-coded.
* Every call raises a typed :class:`~bakerydemo.videos.videogen.exceptions.VideoGenError`
  subclass on failure; callers never see raw ``requests`` exceptions.
* The client is intentionally stateless beyond its session, so it is safe to
  construct per request / per background job.
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
    VideoGenProductionFailed,
    VideoGenTimeoutError,
)

# Terminal workflow-run / export / tool statuses, per the async-patterns skill doc.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
SUCCESS_STATUS = "succeeded"
DEFAULT_BASE_URL = "https://api.videogen.io"


class VideoGenClient:
    """A minimal, typed wrapper over the VideoGen REST API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise VideoGenConfigurationError(
                "VideoGen API key is not configured. Set the VIDEOGEN_API_KEY "
                "environment variable."
            )
        self.api_key = api_key
        # "when it is set, use it verbatim" — do not normalise the override.
        self.base_url = (
            base_url.rstrip("/") if base_url == DEFAULT_BASE_URL else base_url
        )
        self.timeout = timeout
        self._session = session or requests.Session()

    # -- low-level request helpers ---------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        expected: tuple = (200, 201, 202),
    ) -> dict:
        url = self._url(path)
        try:
            response = self._session.request(
                method,
                url,
                json=json,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Could not reach VideoGen at {url}: {exc}"
            ) from exc

        if response.status_code not in expected:
            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            message, code = _extract_error(payload)
            raise VideoGenAPIError(
                f"VideoGen {method} {path} failed with HTTP "
                f"{response.status_code}: {message}",
                status_code=response.status_code,
                code=code,
                payload=payload,
            )

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

    # -- account / connectivity ------------------------------------------

    def get_me(self) -> dict:
        """``GET /v1/me`` — validate the key and return account info. Not billed."""
        return self._request("GET", "/v1/me")

    # -- workflows -------------------------------------------------------

    def create_script_to_video(
        self,
        *,
        script: str,
        visual_style: dict,
        quality: str | None = None,
        remix_actions: list | None = None,
        extra: dict | None = None,
    ) -> dict:
        """``POST /v1/workflows/script-to-video``.

        The ``script`` is used verbatim by VideoGen (no rewriting). Returns the
        202 body: ``{workflowRunId, projectId, projectUrl}``.
        """
        body: dict = {"script": script, "visualStyle": visual_style}
        if quality is not None:
            body["quality"] = quality
        if remix_actions:
            body["remixActions"] = remix_actions
        if extra:
            body.update(extra)
        return self._request("POST", "/v1/workflows/script-to-video", json=body)

    def get_workflow_run(self, workflow_run_id: str) -> dict:
        """``GET /v1/workflows/runs/{id}`` — poll run status."""
        return self._request("GET", f"/v1/workflows/runs/{workflow_run_id}")

    # -- projects / exports ----------------------------------------------

    def export_project(self, project_id: str, options: dict | None = None) -> dict:
        """``POST /v1/projects/{id}/export`` — start a single MP4 export → ``{exportId}``."""
        return self._request(
            "POST", f"/v1/projects/{project_id}/export", json=options or {}
        )

    def get_project_export(self, project_id: str, export_id: str) -> dict:
        """``GET /v1/projects/{id}/exports/{exportId}`` — poll an export."""
        return self._request("GET", f"/v1/projects/{project_id}/exports/{export_id}")

    # -- files -----------------------------------------------------------

    def hydrate_file(self, file_id: str) -> dict:
        """``POST /v1/files/{id}/hydrate`` — refresh signed download URLs."""
        return self._request("POST", f"/v1/files/{file_id}/hydrate")

    def download_bytes(self, signed_url: str) -> bytes:
        """Download the raw bytes of a signed (already-authenticated) URL."""
        try:
            response = self._session.get(signed_url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Failed to download exported file: {exc}"
            ) from exc
        return response.content

    # -- polling helpers -------------------------------------------------

    def poll_workflow_run(
        self,
        workflow_run_id: str,
        *,
        interval: float = 3.0,
        timeout: float = 900.0,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        """Poll a workflow run until it reaches a terminal status.

        Raises :class:`VideoGenProductionFailed` if the run ends in
        ``failed``/``cancelled`` and :class:`VideoGenTimeoutError` on timeout.
        """
        return self._poll(
            lambda: self.get_workflow_run(workflow_run_id),
            interval=interval,
            timeout=timeout,
            on_progress=on_progress,
            what=f"workflow run {workflow_run_id}",
        )

    def poll_project_export(
        self,
        project_id: str,
        export_id: str,
        *,
        interval: float = 3.0,
        timeout: float = 900.0,
        on_progress: Callable[[dict], None] | None = None,
    ) -> dict:
        """Poll a project export until it reaches a terminal status."""
        return self._poll(
            lambda: self.get_project_export(project_id, export_id),
            interval=interval,
            timeout=timeout,
            on_progress=on_progress,
            what=f"export {export_id}",
        )

    def _poll(
        self,
        getter: Callable[[], dict],
        *,
        interval: float,
        timeout: float,
        on_progress: Callable[[dict], None] | None,
        what: str,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            result = getter()
            status = (result.get("status") or "").lower()
            if on_progress is not None:
                on_progress(result)
            if status in TERMINAL_STATUSES:
                if status != SUCCESS_STATUS:
                    raise VideoGenProductionFailed(
                        f"VideoGen {what} ended with status '{status}'.",
                        status=status,
                        provider_error=result.get("error"),
                    )
                return result
            if time.monotonic() >= deadline:
                raise VideoGenTimeoutError(
                    f"VideoGen {what} did not finish within {timeout:.0f}s "
                    f"(last status: '{status or 'unknown'}')."
                )
            time.sleep(interval)


def _extract_error(payload: Any) -> tuple[str, str | None]:
    """Best-effort extraction of a human message and error code from a body."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return (
                str(error.get("message") or error.get("detail") or error),
                error.get("code"),
            )
        message = (
            payload.get("message")
            or payload.get("detail")
            or (str(error) if error is not None else None)
            or str(payload)
        )
        return message, payload.get("code")
    return (str(payload) if payload else "no response body"), None


def get_client(**overrides: Any) -> VideoGenClient:
    """Build a :class:`VideoGenClient` from Django settings.

    Reads ``VIDEOGEN_API_KEY`` and the optional ``VIDEOGEN_BASE_URL`` override
    (both sourced from the environment in settings). Raises
    :class:`VideoGenConfigurationError` if the key is missing.
    """
    api_key = getattr(settings, "VIDEOGEN_API_KEY", "") or ""
    base_url = getattr(settings, "VIDEOGEN_BASE_URL", "") or DEFAULT_BASE_URL
    params: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
    params.update(overrides)
    return VideoGenClient(**params)
