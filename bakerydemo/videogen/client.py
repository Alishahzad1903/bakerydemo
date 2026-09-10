"""A thin, dependency-free HTTP client for the VideoGen REST API.

Only the handful of endpoints this integration needs are wrapped:

* ``POST /v1/workflows/script-to-video`` - start a narrated, stock-footage video
* ``GET  /v1/workflows/runs/{id}``       - poll the generation run
* ``POST /v1/projects/{id}/export``      - start a single 720p export
* ``GET  /v1/projects/{id}/exports/{id}``- poll the export
* download of the finished MP4 from its signed URL

Endpoint shapes, request/response fields and error semantics come solely from
the VideoGen documentation (the ``videogen-docs`` MCP server). Every provider
failure is raised as a typed exception from :mod:`bakerydemo.videogen.exceptions`.

The client is deliberately built on the standard library (``urllib``) so the
integration introduces no new runtime dependency or infrastructure.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .exceptions import (
    VideoGenAPIError,
    VideoGenAuthError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenNotFoundError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenTimeoutError,
)

logger = logging.getLogger("bakerydemo.videogen")

DEFAULT_BASE_URL = "https://api.videogen.io"
DEFAULT_TIMEOUT = 30
# Downloads of the finished MP4 can legitimately take longer than an API call.
DOWNLOAD_TIMEOUT = 120

# An explicit User-Agent is required: VideoGen's edge rejects the stdlib default
# ("Python-urllib/x.y") with HTTP 403, so identify the integration by name.
USER_AGENT = "bakerydemo-videogen/1.0 (+https://github.com/wagtail/bakerydemo)"


class VideoGenClient:
    """Authenticated wrapper around the VideoGen REST API.

    Args:
        api_key: The VideoGen API key, read at runtime from settings/env. Never
            hard-coded and never logged.
        base_url: Optional base address override. When falsy, the documented
            default (``https://api.videogen.io``) is used.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        if not api_key:
            raise VideoGenConfigurationError(
                "VIDEOGEN_API_KEY is not set; cannot talk to VideoGen."
            )
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    # -- Public API --------------------------------------------------------

    def script_to_video(
        self,
        *,
        script: str,
        voice_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a Script-to-Video run with the cheapest documented shape.

        Uses stock footage (never AI imagery), a voice-only narration and the
        default 16:9 aspect ratio (so no post-hoc resize is needed). Returns the
        provider payload, including ``workflowRunId`` and ``projectId``.
        """
        payload: dict[str, Any] = {
            "script": script,
            # Stock footage only - never AI-generated imagery.
            "visualStyle": {"type": "STOCK"},
            # aspectRatio omitted: script-to-video defaults to 16:9.
        }
        if voice_id:
            payload["voiceId"] = voice_id
        return self._request("POST", "/v1/workflows/script-to-video", payload)

    def get_workflow_run(self, workflow_run_id: str) -> dict[str, Any]:
        """Poll a workflow run's status/progress."""
        return self._request("GET", f"/v1/workflows/runs/{workflow_run_id}")

    def export_project(
        self, project_id: str, *, quality: str = "STANDARD"
    ) -> dict[str, Any]:
        """Start a single export of the project at the given quality tier.

        ``quality`` is one of the documented VideoGen tiers (LOW/STANDARD/HIGH/
        MAX); the caller picks the one honouring the spend mandate.
        ``watermarkMode`` / ``endScreenMode`` are left at their ``AUTO`` defaults
        (their ``NONE`` values require a Pro plan and would error).
        """
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/export",
            {"quality": quality},
        )

    def get_export(self, project_id: str, export_id: str) -> dict[str, Any]:
        """Poll an export's status/progress and (when ready) its download URL."""
        return self._request("GET", f"/v1/projects/{project_id}/exports/{export_id}")

    def download_file(self, url: str) -> bytes:
        """Download the finished MP4 from its signed URL.

        The signed URL carries its own credentials, so no Authorization header
        is sent. Network failures are surfaced as typed exceptions.
        """
        request = urllib.request.Request(
            url, method="GET", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise self._api_error_from_http(exc, context="download MP4") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise self._connection_error(exc, context="download MP4") from exc

    # -- Internals ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        logger.debug("VideoGen %s %s", method, path)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise self._api_error_from_http(exc, context=f"{method} {path}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise self._connection_error(exc, context=f"{method} {path}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VideoGenAPIError(
                f"VideoGen returned a non-JSON response for {method} {path}.",
                status=getattr(resp, "status", None),
            ) from exc

    def _connection_error(
        self, exc: Exception, *, context: str
    ) -> VideoGenConnectionError:
        # urllib raises URLError wrapping a socket.timeout for read timeouts.
        reason = getattr(exc, "reason", exc)
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            return VideoGenTimeoutError(f"VideoGen request timed out ({context}).")
        return VideoGenConnectionError(
            f"Could not reach VideoGen ({context}): {reason}"
        )

    def _api_error_from_http(
        self, exc: urllib.error.HTTPError, *, context: str
    ) -> VideoGenAPIError:
        status = exc.code
        message, code, parsed = self._parse_error_body(exc)
        detail = message or f"VideoGen request failed ({context})."

        if status in (401, 403):
            cls: type[VideoGenAPIError] = VideoGenAuthError
        elif status == 404:
            cls = VideoGenNotFoundError
        elif status == 429:
            cls = VideoGenRateLimitError
        elif status >= 500:
            cls = VideoGenServerError
        else:
            cls = VideoGenAPIError

        return cls(detail, status=status, code=code, body=parsed)

    @staticmethod
    def _parse_error_body(
        exc: urllib.error.HTTPError,
    ) -> tuple[str | None, str | None, Any]:
        try:
            raw = exc.read()
        except (OSError, ValueError, AttributeError):  # pragma: no cover - defensive
            return None, None, None
        if not raw:
            return None, None, None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            text = raw.decode("utf-8", "replace")
            return text, None, text
        if isinstance(parsed, dict):
            return parsed.get("message"), parsed.get("code"), parsed
        return None, None, parsed
