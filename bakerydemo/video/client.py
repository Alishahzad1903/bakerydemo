"""
A thin, typed HTTP client for the VideoGen REST API.

Only the endpoints this integration needs are implemented, exactly as described
by the VideoGen documentation:

* ``POST /v1/workflows/script-to-video`` — start a script-to-video workflow
* ``GET  /v1/workflows/runs/{workflowRunId}`` — poll the workflow run
* ``POST /v1/projects/{projectId}/export`` — start an MP4 export
* ``GET  /v1/projects/{projectId}/exports/{exportId}`` — poll the export

Authentication is a bearer token (``Authorization: Bearer <VIDEOGEN_API_KEY>``).
Every failure is raised as a typed :mod:`~bakerydemo.video.exceptions` error.
"""

import logging
import time

import requests
from django.conf import settings

from .exceptions import (
    VideoGenAuthError,
    VideoGenBadRequestError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenNotFoundError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenTimeoutError,
)

logger = logging.getLogger("bakerydemo.video")

# The default VideoGen origin. Overridden verbatim by ``VIDEOGEN_BASE_URL``.
DEFAULT_BASE_URL = "https://api.videogen.io"


class VideoGenClient:
    def __init__(
        self, api_key, base_url=None, *, timeout=30, max_retries=3, session=None
    ):
        if not api_key:
            raise VideoGenConfigurationError(
                "VIDEOGEN_API_KEY is not set; cannot call VideoGen."
            )
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()

    @classmethod
    def from_settings(cls, **kwargs):
        """Build a client from Django settings (which read the environment)."""
        return cls(
            getattr(settings, "VIDEOGEN_API_KEY", None) or None,
            getattr(settings, "VIDEOGEN_BASE_URL", None) or None,
            timeout=getattr(settings, "VIDEOGEN_HTTP_TIMEOUT", 30),
            **kwargs,
        )

    # ------------------------------------------------------------------ #
    # Low-level request handling
    # ------------------------------------------------------------------ #
    def _url(self, path):
        return f"{self.base_url}{path}"

    def _request(self, method, path, *, json=None):
        url = self._url(path)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.request(
                    method, url, json=json, headers=headers, timeout=self.timeout
                )
            except requests.Timeout as exc:
                raise VideoGenTimeoutError(
                    f"VideoGen request timed out: {method} {path}"
                ) from exc
            except requests.RequestException as exc:
                raise VideoGenConnectionError(
                    f"Could not reach VideoGen ({method} {path}): {exc}"
                ) from exc

            # Back off and retry on rate limiting, up to max_retries.
            if response.status_code == 429 and attempt <= self.max_retries:
                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "VideoGen rate limited (%s %s); retrying in %.1fs",
                    method,
                    path,
                    delay,
                )
                time.sleep(delay)
                continue

            if response.status_code >= 400:
                self._raise_for_error(response, method, path)

            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise VideoGenServerError(
                    f"VideoGen returned a non-JSON response for {method} {path}."
                ) from exc

    @staticmethod
    def _retry_delay(response, attempt):
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return min(2.0 ** (attempt - 1), 30.0)

    @staticmethod
    def _raise_for_error(response, method, path):
        message = None
        code = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            message = body.get("message")
            code = body.get("code")
        message = message or (
            f"VideoGen request failed ({response.status_code}) for {method} {path}."
        )
        status = response.status_code
        if status in (401, 403):
            raise VideoGenAuthError(message, status_code=status, code=code)
        if status == 404:
            raise VideoGenNotFoundError(message, status_code=status, code=code)
        if status == 429:
            raise VideoGenRateLimitError(message, status_code=status, code=code)
        if 400 <= status < 500:
            raise VideoGenBadRequestError(message, status_code=status, code=code)
        raise VideoGenServerError(message, status_code=status, code=code)

    # ------------------------------------------------------------------ #
    # VideoGen endpoints
    # ------------------------------------------------------------------ #
    def create_script_to_video(
        self,
        *,
        script,
        visual_style=None,
        voice_id=None,
        aspect_ratio=None,
        language=None,
    ):
        """
        Start a script-to-video workflow. ``visual_style`` defaults to stock
        footage; omitting an actor keeps the narration voice-only. Returns the
        parsed 202 body (``workflowRunId``, ``projectId``, ...).
        """
        payload = {
            "script": script,
            "visualStyle": visual_style or {"type": "STOCK"},
        }
        if voice_id:
            payload["voiceId"] = voice_id
        if aspect_ratio:
            payload["aspectRatio"] = aspect_ratio
        if language:
            payload["language"] = language
        return self._request("POST", "/v1/workflows/script-to-video", json=payload)

    def get_workflow_run(self, workflow_run_id):
        return self._request("GET", f"/v1/workflows/runs/{workflow_run_id}")

    def create_export(self, project_id, *, quality=None):
        """Start an MP4 export of a project. Returns the parsed 202 body."""
        payload = {}
        if quality:
            payload["quality"] = quality
        return self._request("POST", f"/v1/projects/{project_id}/export", json=payload)

    def get_export(self, project_id, export_id):
        return self._request("GET", f"/v1/projects/{project_id}/exports/{export_id}")
