"""A small, typed HTTP client for the VideoGen API.

Only the handful of endpoints this integration needs are implemented, and each
of them is deliberately narrow so the "cheap shape" cost controls (stock
visuals, voiceover only, a single 720p 16:9 export) cannot be accidentally
widened by a convenience wrapper. Every non-2xx response and every network
failure is raised as a typed :mod:`bakerydemo.videos.exceptions` error.

The endpoint surface and request/response shapes used here come from the
VideoGen ``api`` skill (its SKILL.md, reference files, and the OpenAPI spec it
points to) — the sole reference for talking to VideoGen.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .exceptions import (
    VideoGenAPIError,
    VideoGenAuthError,
    VideoGenConnectionError,
    VideoGenInsufficientCreditsError,
)

logger = logging.getLogger("bakerydemo.videos")

# Terminal states shared by workflow runs and project exports (VideoGen
# ``JobStatus``): pending, running, succeeded, failed, cancelled.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class VideoGenClient:
    """Thin wrapper over the VideoGen REST API.

    Parameters
    ----------
    api_key:
        Bearer token for the VideoGen account. Read from the environment via
        Django settings; never hard-coded.
    base_url:
        API base address. ``https://api.videogen.io`` by default, or the
        verbatim ``VIDEOGEN_BASE_URL`` override when configured.
    timeout:
        Per-request socket timeout in seconds.
    """

    def __init__(self, api_key: str, base_url: str, *, timeout: int = 60) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            }
        )

    # -- low level ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = self._session.request(
                method, url, json=json, timeout=self._timeout
            )
        except requests.RequestException as exc:  # connection/timeout/etc.
            raise VideoGenConnectionError(
                f"Could not reach VideoGen at {url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise self._error_from_response(response)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:  # malformed body from provider
            raise VideoGenAPIError(
                "VideoGen returned a non-JSON response",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _error_from_response(response: requests.Response) -> VideoGenAPIError:
        """Build a typed error from VideoGen's standard error body."""
        message = f"VideoGen request failed with HTTP {response.status_code}"
        code: str | None = None
        internal_code: str | None = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            message = body.get("message") or message
            code = body.get("code")
            internal_code = body.get("internalErrorCode")

        status = response.status_code
        kwargs = {
            "status_code": status,
            "code": code,
            "internal_error_code": internal_code,
        }
        if status in (401, 403):
            return VideoGenAuthError(message, **kwargs)
        if status == 429:
            return VideoGenInsufficientCreditsError(message, **kwargs)
        return VideoGenAPIError(message, **kwargs)

    # -- account -----------------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        """Return the account/team behind the API key. Useful as a key test."""
        return self._request("GET", "/v1/me")

    # -- workflows ---------------------------------------------------------

    def create_script_to_video(
        self,
        *,
        script: str,
        visual_style: str,
        aspect_width: int,
        aspect_height: int,
    ) -> dict[str, Any]:
        """Start a script-to-video workflow run (HTTP 202).

        The script is narrated verbatim. Only the cheap shape is requested:
        stock visuals, a voiceover with the account default voice (no
        ``actorEntityId`` => no avatar/presenter), and no remix actions. The
        returned dict contains ``workflowRunId`` and ``projectId``.
        """
        payload = {
            "script": script,
            "visualStyle": {"type": visual_style},
            "aspectRatio": {"width": aspect_width, "height": aspect_height},
        }
        return self._request("POST", "/v1/workflows/script-to-video", json=payload)

    def get_workflow_run(self, workflow_run_id: str) -> dict[str, Any]:
        """Poll one workflow run.

        Returns ``status``, ``progressPercentage``, ``projectId`` and ``error``.
        """
        return self._request("GET", f"/v1/workflows/runs/{workflow_run_id}")

    # -- projects / export -------------------------------------------------

    def export_project(self, project_id: str, *, quality: str) -> dict[str, Any]:
        """Start a single MP4 export of a project (HTTP 202).

        ``quality`` is a vertical resolution tier; ``STANDARD`` is 720p (the
        ladder ascends STANDARD 720p -> HIGH 1080p -> FULL_HIGH 1440p ->
        ULTRA_HIGH 4K). The project's 16:9 aspect ratio is kept as-is (no
        resize). Returns ``exportId``.
        """
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/export",
            json={"quality": quality},
        )

    def get_project_export(self, project_id: str, export_id: str) -> dict[str, Any]:
        """Poll one project export.

        Returns ``status``, ``progressPercentage``, ``downloadUrl`` (a signed
        MP4 URL when succeeded), ``exportFileId`` and ``error``.
        """
        return self._request("GET", f"/v1/projects/{project_id}/exports/{export_id}")

    # -- files -------------------------------------------------------------

    def hydrate_file(self, file_id: str) -> dict[str, Any]:
        """Refresh the signed download URLs for a file."""
        return self._request("POST", f"/v1/files/{file_id}/hydrate")

    def download_to(self, url: str, destination) -> int:
        """Stream a (signed) URL to a writable binary file object.

        Returns the number of bytes written. Raises
        :class:`VideoGenConnectionError` on a network failure and
        :class:`VideoGenAPIError` on a non-2xx response.
        """
        try:
            with self._session.get(url, stream=True, timeout=self._timeout) as response:
                if response.status_code >= 400:
                    raise self._error_from_response(response)
                written = 0
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        destination.write(chunk)
                        written += len(chunk)
                return written
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Could not download from VideoGen: {exc}"
            ) from exc
