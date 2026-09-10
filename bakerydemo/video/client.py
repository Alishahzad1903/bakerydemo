"""A thin, typed HTTP client for the VideoGen REST API.

Only the endpoints this integration needs are implemented, and every one of them
is shaped for the *cheapest* possible video (see :mod:`bakerydemo.video.services`):

* ``POST /v1/workflows/script-to-video`` - start production from a script, using
  **stock footage only** and a **voice-only** narration (no avatar/presenter).
* ``GET  /v1/workflows/runs/{workflowRunId}`` - poll the async production.
* ``POST /v1/projects/{projectId}/export`` - export **once**, at **720p**.
* ``GET  /v1/projects/{projectId}/exports/{exportId}`` - poll the export and get a
  fresh signed ``downloadUrl``.
* ``GET  /v1/me`` - a cheap credentials check (no media produced).

All request/transport failures are raised as :class:`~bakerydemo.video.exceptions.VideoGenError`
subclasses. This client deliberately performs **no** media-producing calls beyond
the four above, and never retries a workflow or export, so it cannot double-bill.

Reference: the VideoGen documentation, accessed via the ``videogen-docs`` MCP server.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

from .exceptions import (
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenError,
    error_for_status,
)

logger = logging.getLogger("bakerydemo.video")

DEFAULT_BASE_URL = "https://api.videogen.io"

# Terminal statuses shared by workflow runs and exports.
TERMINAL_SUCCESS = "succeeded"
TERMINAL_FAILURE = {"failed", "cancelled"}


class VideoGenClient:
    """Authenticated client for a single VideoGen account."""

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
                "VIDEOGEN_API_KEY is not configured; set it in the environment."
            )
        self.api_key = api_key
        # Use the configured base URL verbatim (only trimming a trailing slash so
        # paths join cleanly); an operator-supplied VIDEOGEN_BASE_URL wins.
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()

    @classmethod
    def from_settings(cls, session: requests.Session | None = None) -> VideoGenClient:
        """Build a client from Django settings (env-sourced, never hard-coded)."""
        return cls(
            api_key=getattr(settings, "VIDEOGEN_API_KEY", None) or "",
            base_url=getattr(settings, "VIDEOGEN_BASE_URL", None) or DEFAULT_BASE_URL,
            timeout=float(getattr(settings, "VIDEOGEN_REQUEST_TIMEOUT", 30.0)),
            session=session,
        )

    # -- transport ---------------------------------------------------------

    def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        try:
            response = self._session.request(
                method,
                url,
                json=json,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise VideoGenConnectionError(
                f"VideoGen request timed out after {self.timeout}s: {method} {path}"
            ) from exc
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Could not reach VideoGen: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise self._error_from_response(response, method, path)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise VideoGenError(
                f"VideoGen returned a non-JSON response for {method} {path}"
            ) from exc

    @staticmethod
    def _error_from_response(
        response: requests.Response, method: str, path: str
    ) -> VideoGenError:
        message = f"VideoGen {method} {path} failed"
        code = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            # VideoGen ApiError schema: {"message": ..., "code": ...}
            message = body.get("message") or message
            code = body.get("code")
        return error_for_status(response.status_code, message, code)

    # -- endpoints ---------------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        """Confirm the API key is valid (``GET /v1/me``). Produces no media."""
        return self._request("GET", "/v1/me")

    def create_script_to_video(
        self,
        *,
        script: str,
        aspect_ratio: dict[str, int] | None = None,
        visual_style: dict[str, Any] | None = None,
        language: str | None = None,
        voice_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a script-to-video workflow run.

        The default shape is the cheap one this integration mandates: 16:9 stock
        footage visuals and a voice-only narration. No avatar/presenter, no
        AI imagery, no featured b-roll, no remix actions.

        ``aspectRatio`` is a ``{"width", "height"}`` ratio object (not a string),
        per the VideoGen API; it defaults to 16:9.
        """
        payload: dict[str, Any] = {
            "script": script,
            "aspectRatio": aspect_ratio or {"width": 16, "height": 9},
            "visualStyle": visual_style or {"type": "STOCK"},
        }
        if language:
            payload["language"] = language
        if voice_id:
            payload["voiceId"] = voice_id
        return self._request(
            "POST", "/v1/workflows/script-to-video", json=payload
        )

    def get_workflow_run(self, workflow_run_id: str) -> dict[str, Any]:
        """Poll a workflow run (``GET /v1/workflows/runs/{id}``)."""
        return self._request(
            "GET", f"/v1/workflows/runs/{workflow_run_id}"
        )

    # VideoGen's export quality tiers (from the docs). The docs don't map them to
    # pixel resolutions, but the names mirror the standard tiers:
    #   STANDARD = SD (~480p), HIGH = HD (720p), FULL_HIGH = Full HD (1080p),
    #   ULTRA_HIGH = Ultra HD (4K).
    # We export at 720p as mandated -> HIGH. We never use ULTRA_HIGH (4K).
    QUALITY_720P = "HIGH"

    def export_project(
        self, project_id: str, *, quality: str = QUALITY_720P
    ) -> dict[str, Any]:
        """Export a project to MP4 (``POST /v1/projects/{id}/export``).

        Called **exactly once** per project, at 720p (``HIGH``). Watermark and
        end-screen modes are left at their account defaults to avoid
        Pro-plan-gated failures.
        """
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/export",
            json={"quality": quality},
        )

    def get_export(self, project_id: str, export_id: str) -> dict[str, Any]:
        """Poll an export and get a fresh signed ``downloadUrl`` when ready.

        (``GET /v1/projects/{id}/exports/{export_id}``.) Re-fetching here is a
        read - it never triggers a second export.
        """
        return self._request(
            "GET", f"/v1/projects/{project_id}/exports/{export_id}"
        )
