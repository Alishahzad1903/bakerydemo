"""Minimal, dependency-free HTTP client for the VideoGen API.

Built only from the ``videogen-docs`` reference. It speaks the three calls the
integration needs:

* ``POST /v1/workflows/script-to-video`` - start a narrated stock-footage video.
* ``GET  /v1/workflows/runs/{workflowRunId}`` - poll a run to a terminal state.
* ``POST /v1/projects/{projectId}/export`` - render the project to MP4.
* ``GET  /v1/projects/{projectId}/exports/{exportId}`` - poll the export and
  read the (freshly re-signed) download URL.

The client is deliberately built on the standard library (``urllib``) so the
integration adds no new runtime dependency. Authentication is a bearer token
and the base URL both come from Django settings, never from a hard-coded value.
Every non-2xx response is raised as a typed :class:`VideoGenError`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

from .exceptions import (
    VideoGenConnectionError,
    VideoGenError,
    VideoGenRateLimitError,
    VideoGenServerError,
    error_for_status,
)

DEFAULT_BASE_URL = "https://api.videogen.io"

# A descriptive User-Agent. The default ``Python-urllib/x.y`` agent is rejected
# with HTTP 403 by VideoGen's edge/WAF, so we must send an explicit one.
USER_AGENT = "bakerydemo-videogen-integration/1.0"

# Terminal statuses shared by workflow runs and project exports.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class VideoGenClient:
    """A thin, synchronous VideoGen API client.

    Args:
        api_key: Bearer token. Required; a blank key raises immediately so the
            failure is attributed clearly rather than surfacing as a 401 later.
        base_url: API base address. Defaults to the documented production host.
        timeout: Per-request socket timeout, in seconds.
        max_retries: How many times to retry a request that failed with a
            retriable condition (429, 5xx, or a transport error).
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise VideoGenError(
                "VideoGen API key is not configured (set the VIDEOGEN_API_KEY "
                "environment variable)."
            )
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    # -- HTTP plumbing -----------------------------------------------------

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            attempt += 1
            try:
                return self._perform(method, url, data, headers)
            except (
                VideoGenRateLimitError,
                VideoGenServerError,
                VideoGenConnectionError,
            ):
                if attempt > self.max_retries:
                    raise
                # Exponential backoff: 1s, 2s, 4s, ... as recommended for 429s.
                time.sleep(min(2 ** (attempt - 1), 8))
                continue

    def _perform(
        self,
        method: str,
        url: str,
        data: bytes | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return self._parse_body(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            message, code = self._parse_error(raw)
            raise error_for_status(exc.code, message, code) from exc
        except urllib.error.URLError as exc:
            raise VideoGenConnectionError(
                f"Could not reach VideoGen at {url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise VideoGenConnectionError(
                f"Timed out contacting VideoGen at {url}."
            ) from exc

    @staticmethod
    def _parse_body(raw: bytes) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise VideoGenError(
                "VideoGen returned a response that was not valid JSON."
            ) from exc
        if not isinstance(parsed, dict):
            return {"data": parsed}
        return parsed

    @staticmethod
    def _parse_error(raw: bytes) -> tuple[str, str | None]:
        """Extract ``(message, code)`` from an ``ApiError`` body, best-effort."""
        message = "VideoGen request failed."
        code = None
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                message = parsed.get("message") or message
                code = parsed.get("code")
        return message, code

    # -- API operations ----------------------------------------------------

    def create_script_to_video(
        self,
        *,
        script: str,
        visual_style: dict[str, Any],
        aspect_ratio: dict[str, Any],
    ) -> dict[str, Any]:
        """Start a script-to-video workflow. Returns the 202 body.

        The body carries ``workflowRunId`` (poll target) and ``projectId``
        (export target).
        """
        return self._request(
            "POST",
            "/v1/workflows/script-to-video",
            {
                "script": script,
                "visualStyle": visual_style,
                "aspectRatio": aspect_ratio,
            },
        )

    def get_workflow_run(self, workflow_run_id: str) -> dict[str, Any]:
        """Fetch a workflow run's current state (``status``, ``progressPercentage``, ...)."""
        return self._request("GET", f"/v1/workflows/runs/{workflow_run_id}")

    def export_project(self, project_id: str, *, quality: str) -> dict[str, Any]:
        """Start a project export at ``quality``. Returns a body with ``exportId``."""
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/export",
            {"quality": quality},
        )

    def get_project_export(self, project_id: str, export_id: str) -> dict[str, Any]:
        """Fetch an export's state. When ``succeeded`` it carries ``downloadUrl``.

        Per the docs this endpoint re-signs the download URL when it is close to
        expiring, so calling it is also how the site keeps the MP4 reachable.
        """
        return self._request("GET", f"/v1/projects/{project_id}/exports/{export_id}")


def get_client() -> VideoGenClient:
    """Build a client from the site's settings (never from a hard-coded value)."""
    return VideoGenClient(
        api_key=getattr(settings, "VIDEOGEN_API_KEY", "") or "",
        base_url=getattr(settings, "VIDEOGEN_BASE_URL", None),
    )
