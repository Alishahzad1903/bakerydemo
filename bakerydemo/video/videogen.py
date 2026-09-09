"""A thin, typed HTTP client for the VideoGen REST API (v1).

Everything this client knows about VideoGen comes from the official docs
(https://docs.videogen.io). The endpoints used are:

* ``POST /v1/workflows/script-to-video``        -> start a narrated video
* ``GET  /v1/workflows/runs/{workflowRunId}``   -> poll the workflow run
* ``POST /v1/projects/{projectId}/export``      -> render the finished MP4
* ``GET  /v1/projects/{projectId}/exports/{exportId}`` -> poll the export

Authentication is a bearer token in the ``Authorization`` header. Failures are
translated into the typed exceptions in :mod:`bakerydemo.video.exceptions`.

The client deliberately performs *single* requests only — polling loops live in
:mod:`bakerydemo.video.service`, so this class stays easy to reason about and to
mock in tests.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

from .exceptions import (
    VideoGenConfigurationError,
    VideoGenError,
    VideoGenTransportError,
    error_for_status,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.videogen.io"

# Request timeouts (connect, read) in seconds. Kept modest — every call here is
# either a quick create/poll or a short JSON read; the long-running work happens
# provider-side and is observed by polling, not by holding a connection open.
DEFAULT_TIMEOUT = (10, 60)


class VideoGenClient:
    """Minimal client for the subset of the VideoGen API this site needs."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: Any = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise VideoGenConfigurationError(
                "No VideoGen API key configured. Set the VIDEOGEN_API_KEY "
                "environment variable."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()

    @classmethod
    def from_settings(cls, **kwargs: Any) -> VideoGenClient:
        """Build a client from Django settings (which read the environment).

        ``VIDEOGEN_API_KEY`` supplies the credential and ``VIDEOGEN_BASE_URL``,
        when set, overrides the API base address verbatim.
        """
        base_url = getattr(settings, "VIDEOGEN_BASE_URL", None) or DEFAULT_BASE_URL
        return cls(
            api_key=getattr(settings, "VIDEOGEN_API_KEY", None) or "",
            base_url=base_url,
            **kwargs,
        )

    # -- low level ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
    ) -> dict:
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
        except requests.RequestException as exc:
            raise VideoGenTransportError(
                f"Could not reach VideoGen ({method} {path}): {exc}"
            ) from exc

        if response.status_code >= 400:
            raise self._error_from_response(method, path, response)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise VideoGenTransportError(
                f"VideoGen returned a non-JSON response for {method} {path}."
            ) from exc

    @staticmethod
    def _error_from_response(
        method: str, path: str, response: requests.Response
    ) -> VideoGenError:
        message = None
        code = None
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = (response.text or "").strip() or None
        if isinstance(body, dict):
            # VideoGen error bodies follow the ApiError schema: {message, code?}.
            message = body.get("message")
            code = body.get("code")
        message = message or f"VideoGen request failed ({method} {path})."
        return error_for_status(response.status_code, message, code=code, body=body)

    # -- workflows ---------------------------------------------------------

    @staticmethod
    def _aspect_ratio_object(aspect_ratio: str) -> dict:
        """Convert a ``"W:H"`` ratio string to VideoGen's object form.

        VideoGen expects ``aspectRatio`` as ``{"width": W, "height": H}`` (a
        ratio pair, not pixel dimensions), not a bare string.
        """
        width, height = aspect_ratio.split(":")
        return {"width": int(width), "height": int(height)}

    def create_script_to_video(
        self,
        *,
        script: str,
        aspect_ratio: str = "16:9",
        visual_style_type: str = "STOCK",
        voice_id: str | None = None,
    ) -> dict:
        """Start the script-to-video workflow.

        The request is intentionally the *cheapest possible* shape: stock
        footage only (never AI-generated imagery), a voice-only narration (no
        avatar/presenter), and a 16:9 aspect ratio. No ``remixActions``,
        ``actorEntityId``, ``scenes`` or image-to-video conversion are sent, so
        nothing beyond the single video is generated or billed. Returns the raw
        response which includes ``workflowRunId`` and ``projectId``.
        """
        payload: dict[str, Any] = {
            "script": script,
            "visualStyle": {"type": visual_style_type},
            "aspectRatio": self._aspect_ratio_object(aspect_ratio),
        }
        if voice_id:
            payload["voiceId"] = voice_id
        return self._request("POST", "/v1/workflows/script-to-video", json=payload)

    def get_workflow_run(self, workflow_run_id: str) -> dict:
        """Fetch the current state of a workflow run."""
        return self._request("GET", f"/v1/workflows/runs/{workflow_run_id}")

    # -- exports -----------------------------------------------------------

    def export_project(self, project_id: str, *, quality: str = "STANDARD") -> dict:
        """Start a single MP4 export of a finished project.

        ``quality="STANDARD"`` selects the lowest (720p-class) tier — never 4K.
        ``watermarkMode`` and
        ``endScreenMode`` are left at their ``AUTO`` defaults so the export
        succeeds on any plan. Returns the raw response which includes
        ``exportId``.
        """
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/export",
            json={"quality": quality},
        )

    def get_project_export(self, project_id: str, export_id: str) -> dict:
        """Fetch the current state of a project export."""
        return self._request("GET", f"/v1/projects/{project_id}/exports/{export_id}")

    # -- download ----------------------------------------------------------

    def download_file(self, download_url: str) -> bytes:
        """Download the exported MP4 from a VideoGen signed URL.

        The signed URL already carries its own authorization, so we must *not*
        attach the API bearer token here.
        """
        try:
            response = self._session.get(
                download_url, timeout=self.timeout, stream=True
            )
        except requests.RequestException as exc:
            raise VideoGenTransportError(
                f"Could not download the exported video: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise VideoGenTransportError(
                f"Downloading the exported video failed (HTTP {response.status_code}).",
                status=response.status_code,
            )
        return response.content
