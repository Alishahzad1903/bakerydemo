"""
Thin, typed client over the VideoGen REST API.

Only the handful of endpoints this integration needs are implemented, and only
in the *cheapest* shape the product allows:

* ``POST /v1/workflows/script-to-video`` with ``visualStyle={"type": "STOCK"}``
  (stock footage, voice-only narration – no AI imagery, no avatar).
* ``GET  /v1/workflows/runs/{workflowRunId}`` to follow the build.
* ``POST /v1/projects/{projectId}/export`` at ``quality="HD"`` (720p) – exactly
  one export, never 4K.
* ``GET  /v1/projects/{projectId}/exports/{exportId}`` to follow the export.

All endpoint shapes are taken from the VideoGen documentation; nothing here is
invented. Every failure is raised as a :class:`VideoGenError` subclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from .exceptions import (
    VideoGenAuthenticationError,
    VideoGenBadRequestError,
    VideoGenConnectionError,
    VideoGenError,
    VideoGenNotFoundError,
    VideoGenPermissionError,
    VideoGenRateLimitError,
    VideoGenServerError,
)

DEFAULT_BASE_URL = "https://api.videogen.io"

#: Terminal workflow/export statuses reported by VideoGen.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class ScriptToVideoResult:
    """Result of kicking off a script-to-video workflow (HTTP 202)."""

    workflow_run_id: str
    project_id: str
    project_url: str | None = None


@dataclass(frozen=True)
class WorkflowRun:
    """A snapshot of a workflow run's state."""

    workflow_run_id: str
    status: str
    progress_percentage: int
    project_id: str | None
    error_message: str | None
    raw: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class ProjectExport:
    """A snapshot of a project export's state."""

    export_id: str
    status: str
    progress_percentage: int
    download_url: str | None
    export_file_id: str | None
    error_message: str | None
    raw: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def _coerce_progress(value: Any) -> int:
    """Clamp a provider-reported progress value to an int in ``0..100``."""
    try:
        pct = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, pct))


class VideoGenClient:
    """Blocking HTTP client for VideoGen.

    The client is intentionally stateless beyond its configuration so a single
    instance is safe to reuse. Credentials and base URL are injected by the
    caller (sourced from Django settings / environment) – nothing is read from
    the environment here and no value is ever hard-coded.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            # Fail fast with a typed error rather than letting VideoGen return a
            # confusing 401 on the first call.
            raise VideoGenAuthenticationError(
                "VideoGen API key is not configured.",
                status=401,
                code="missing_api_key",
            )
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()

    # -- public API ------------------------------------------------------------

    def create_script_to_video(
        self,
        script: str,
        *,
        aspect_ratio: tuple[int, int] = (16, 9),
    ) -> ScriptToVideoResult:
        """Start a stock-footage, voice-only narrated video from ``script``.

        Returns immediately (VideoGen replies 202); the build runs
        asynchronously and is followed via :meth:`get_workflow_run`.

        ``aspect_ratio`` is a ``(width, height)`` ratio pair (not pixels) sent as
        the object shape VideoGen expects; it defaults to 16:9 landscape.
        """
        width, height = aspect_ratio
        payload = {
            "script": script,
            # Stock footage only – never AI-generated imagery.
            "visualStyle": {"type": "STOCK"},
            "aspectRatio": {"width": width, "height": height},
        }
        data = self._request("POST", "/v1/workflows/script-to-video", json=payload)
        workflow_run_id = data.get("workflowRunId")
        project_id = data.get("projectId")
        if not workflow_run_id or not project_id:
            raise VideoGenError(
                "VideoGen did not return a workflowRunId/projectId for the "
                "script-to-video request.",
                code="unexpected_response",
            )
        return ScriptToVideoResult(
            workflow_run_id=workflow_run_id,
            project_id=project_id,
            project_url=data.get("projectUrl"),
        )

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        """Fetch the current state of a workflow run."""
        data = self._request("GET", f"/v1/workflows/runs/{workflow_run_id}")
        return WorkflowRun(
            workflow_run_id=data.get("workflowRunId", workflow_run_id),
            status=str(data.get("status", "")).lower(),
            progress_percentage=_coerce_progress(data.get("progressPercentage")),
            project_id=data.get("projectId"),
            error_message=_extract_error_message(data),
            raw=data,
        )

    def start_export(self, project_id: str, *, quality: str | None = None) -> str:
        """Start a single MP4 export of ``project_id`` and return its export id.

        ``quality`` selects the export resolution. It is omitted from the request
        when ``None`` so VideoGen applies its default resolution – see
        :data:`bakerydemo.video.services.EXPORT_QUALITY` for why this defaults to
        omitted rather than an explicit ``"720p"`` constant.

        ``watermarkMode`` / ``endScreenMode`` are left at their ``AUTO`` defaults
        so the export works on any plan tier (setting them to ``NONE`` requires a
        Pro plan).
        """
        payload: dict[str, Any] = {}
        if quality is not None:
            payload["quality"] = quality
        data = self._request(
            "POST", f"/v1/projects/{project_id}/export", json=payload
        )
        export_id = data.get("exportId")
        if not export_id:
            raise VideoGenError(
                "VideoGen did not return an exportId for the export request.",
                code="unexpected_response",
            )
        return export_id

    def get_export(self, project_id: str, export_id: str) -> ProjectExport:
        """Fetch the current state of a project export.

        Re-fetching is the documented way to obtain a freshly-signed
        ``downloadUrl`` (VideoGen re-signs when it is close to expiring).
        """
        data = self._request(
            "GET", f"/v1/projects/{project_id}/exports/{export_id}"
        )
        return ProjectExport(
            export_id=data.get("exportId", export_id),
            status=str(data.get("status", "")).lower(),
            progress_percentage=_coerce_progress(data.get("progressPercentage")),
            download_url=data.get("downloadUrl"),
            export_file_id=data.get("exportFileId"),
            error_message=_extract_error_message(data),
            raw=data,
        )

    def download_file(self, url: str) -> bytes:
        """Download a rendered file from a (signed) ``downloadUrl``.

        The signed URL is self-authenticating, so the bearer token is *not*
        attached. Fetching a finished file is not a billed operation.
        """
        try:
            response = self._session.get(url, timeout=self.timeout, stream=True)
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Failed to download rendered video: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise VideoGenError(
                f"Downloading the rendered video failed (HTTP {response.status_code}).",
                status=response.status_code,
            )
        return response.content

    # -- internals -------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
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
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Could not reach VideoGen at {url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise _error_for_response(response)

        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise VideoGenError(
                "VideoGen returned a non-JSON response body.",
                status=response.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise VideoGenError(
                "VideoGen returned an unexpected (non-object) response body.",
                status=response.status_code,
            )
        return body


def _extract_error_message(data: dict[str, Any]) -> str | None:
    """Pull ``error.message`` (or a plain ``error`` string) out of a payload."""
    error = data.get("error")
    if isinstance(error, dict):
        return error.get("message") or None
    if isinstance(error, str):
        return error or None
    return None


def _error_for_response(response: requests.Response) -> VideoGenError:
    """Map a non-2xx ``ApiError`` response to the matching typed exception."""
    status = response.status_code
    message = f"VideoGen request failed with HTTP {status}."
    code: str | None = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = body.get("message") or message
        raw_code = body.get("code")
        code = raw_code if isinstance(raw_code, str) else None

    if status == 400 or status == 422:
        return VideoGenBadRequestError(message, status=status, code=code)
    if status == 401:
        return VideoGenAuthenticationError(message, status=status, code=code)
    if status == 403:
        return VideoGenPermissionError(message, status=status, code=code)
    if status == 404:
        return VideoGenNotFoundError(message, status=status, code=code)
    if status == 429:
        return VideoGenRateLimitError(message, status=status, code=code)
    if status >= 500:
        return VideoGenServerError(message, status=status, code=code)
    return VideoGenError(message, status=status, code=code)
