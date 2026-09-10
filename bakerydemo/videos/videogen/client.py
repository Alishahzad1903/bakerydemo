"""HTTP client for the VideoGen API.

Implements just the slice of https://docs.videogen.io that the
article-to-video flow needs:

* ``POST /v1/workflows/script-to-video``           - start a script-to-video run
* ``GET  /v1/workflows/runs/{workflowRunId}``       - poll the run
* ``POST /v1/projects/{projectId}/export``          - export the project to MP4
* ``GET  /v1/projects/{projectId}/exports/{id}``    - poll the export
* download the signed MP4 URL

Authentication is a bearer API key in the ``Authorization`` header. The base URL
and key are read from Django settings (and therefore from the environment);
nothing is hard-coded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests
from django.conf import settings

from .exceptions import (
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenError,
    api_error_for_status,
)

DEFAULT_BASE_URL = "https://api.videogen.io"

# Terminal statuses shared by workflow runs and project exports.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class WorkflowRun:
    """Parsed ``GET /v1/workflows/runs/{id}`` response (fields we rely on)."""

    workflow_run_id: str
    status: str
    progress_percentage: int
    project_id: str | None = None
    error: Any = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


@dataclass(frozen=True)
class ProjectExport:
    """Parsed ``GET /v1/projects/{id}/exports/{id}`` response."""

    export_id: str
    project_id: str
    status: str
    progress_percentage: int
    download_url: str | None = None
    error: Any = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


def _error_message(body: Any, default: str) -> tuple[str, str | None]:
    """Extract ``(message, code)`` from a VideoGen ``ApiError`` body."""
    if isinstance(body, dict):
        message = body.get("message") or default
        code = body.get("code")
        return str(message), (str(code) if code else None)
    return default, None


class VideoGenClient:
    """A thin, typed wrapper over the VideoGen REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise VideoGenConfigurationError(
                "VideoGen API key is not configured. Set the VIDEOGEN_API_KEY "
                "environment variable."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._session = session or requests.Session()

    # -- low-level request handling -----------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _sleep(self, attempt: int) -> None:
        # Exponential backoff: 1s, 2s, 4s, ... (scaled by backoff_factor).
        time.sleep(self.backoff_factor * (2**attempt))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        idempotent: bool,
    ) -> dict:
        """Perform a request and return the parsed JSON body.

        Retry policy is billing-safe:

        * Idempotent GETs retry on ``429`` and ``5xx`` and on network errors.
        * Mutating POSTs (which create/export videos and are billed) retry
          ONLY on ``429`` - a rate-limited request is known not to have been
          processed. They are never retried on ``5xx`` or network errors,
          whose outcome is ambiguous, so a video is never produced twice.
        """
        url = f"{self.base_url}{path}"
        headers = self._headers()
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        last_exc: VideoGenError | None = None
        for attempt in range(self.max_retries + 1):
            can_retry = attempt < self.max_retries
            try:
                response = self._session.request(
                    method,
                    url,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                # No HTTP response was produced.
                if idempotent and can_retry:
                    self._sleep(attempt)
                    continue
                raise VideoGenConnectionError(
                    f"Could not reach VideoGen at {url}: {exc}"
                ) from exc

            if 200 <= response.status_code < 300:
                return self._parse_json(response)

            body = self._safe_json(response)
            message, code = _error_message(
                body, f"VideoGen request to {path} failed"
            )
            status = response.status_code

            retry_this = can_retry and (
                status == 429 or (idempotent and 500 <= status < 600)
            )
            if retry_this:
                last_exc = api_error_for_status(
                    status, message, code=code, body=body
                )
                self._sleep(attempt)
                continue

            raise api_error_for_status(status, message, code=code, body=body)

        # Exhausted retries on a retryable error.
        assert last_exc is not None  # for type-checkers; loop guarantees it
        raise last_exc

    @staticmethod
    def _parse_json(response: requests.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise VideoGenError(
                "VideoGen returned a non-JSON success response."
            ) from exc
        if not isinstance(data, dict):
            raise VideoGenError("VideoGen returned an unexpected response shape.")
        return data

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"message": (response.text or "").strip()[:500]}

    # -- high-level API ------------------------------------------------------

    def create_script_to_video(
        self,
        *,
        script: str,
        aspect_ratio: dict,
        visual_style: dict,
        quality: str | None = None,
        voice_id: str | None = None,
        language: str | None = None,
        visual_pacing: str | None = None,
    ) -> dict:
        """Start a script-to-video workflow run.

        Returns the raw 202 body, which includes ``workflowRunId`` and
        ``projectId``. This call is billed, so it is never retried except on
        an explicit ``429``.
        """
        payload: dict[str, Any] = {
            "script": script,
            "aspectRatio": aspect_ratio,
            "visualStyle": visual_style,
        }
        if quality is not None:
            payload["quality"] = quality
        if voice_id is not None:
            payload["voiceId"] = voice_id
        if language is not None:
            payload["language"] = language
        if visual_pacing is not None:
            payload["visualPacing"] = visual_pacing
        return self._request(
            "POST",
            "/v1/workflows/script-to-video",
            json_body=payload,
            idempotent=False,
        )

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        data = self._request(
            "GET",
            f"/v1/workflows/runs/{workflow_run_id}",
            idempotent=True,
        )
        return WorkflowRun(
            workflow_run_id=str(data.get("workflowRunId", workflow_run_id)),
            status=str(data.get("status", "")),
            progress_percentage=int(data.get("progressPercentage") or 0),
            project_id=data.get("projectId"),
            error=data.get("error"),
            raw=data,
        )

    def export_project(
        self,
        project_id: str,
        *,
        quality: str | None = None,
        watermark_mode: str | None = None,
        end_screen_mode: str | None = None,
    ) -> dict:
        """Start a single MP4 export of a project.

        Returns the raw body including ``exportId``. Billed, so never retried
        except on an explicit ``429``.
        """
        payload: dict[str, Any] = {}
        if quality is not None:
            payload["quality"] = quality
        if watermark_mode is not None:
            payload["watermarkMode"] = watermark_mode
        if end_screen_mode is not None:
            payload["endScreenMode"] = end_screen_mode
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/export",
            json_body=payload,
            idempotent=False,
        )

    def get_export(self, project_id: str, export_id: str) -> ProjectExport:
        data = self._request(
            "GET",
            f"/v1/projects/{project_id}/exports/{export_id}",
            idempotent=True,
        )
        return ProjectExport(
            export_id=str(data.get("exportId", export_id)),
            project_id=str(data.get("projectId", project_id)),
            status=str(data.get("status", "")),
            progress_percentage=int(data.get("progressPercentage") or 0),
            download_url=data.get("downloadUrl"),
            error=data.get("error"),
            raw=data,
        )

    def stream_download(self, url: str, *, chunk_size: int = 1024 * 256) -> Iterator[bytes]:
        """Stream the bytes of a signed download URL.

        The signed URL is served from VideoGen's own storage (not the API), so
        no ``Authorization`` header is sent.
        """
        try:
            response = self._session.get(url, stream=True, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise VideoGenConnectionError(
                f"Could not download the exported video: {exc}"
            ) from exc
        if not (200 <= response.status_code < 300):
            body = self._safe_json(response)
            message, code = _error_message(body, "Downloading the video failed")
            raise api_error_for_status(
                response.status_code, message, code=code, body=body
            )
        return response.iter_content(chunk_size=chunk_size)


def get_client() -> VideoGenClient:
    """Build a :class:`VideoGenClient` from Django settings (and the env)."""
    return VideoGenClient(
        api_key=getattr(settings, "VIDEOGEN_API_KEY", "") or "",
        base_url=getattr(settings, "VIDEOGEN_BASE_URL", None) or DEFAULT_BASE_URL,
        timeout=float(getattr(settings, "VIDEOGEN_REQUEST_TIMEOUT_SECONDS", 30.0)),
    )
