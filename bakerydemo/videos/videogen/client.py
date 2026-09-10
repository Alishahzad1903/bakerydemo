"""A thin, typed HTTP client for the VideoGen API.

Everything the site knows about VideoGen's wire protocol lives here. The client
covers exactly the calls this integration needs:

- ``create_script_to_video`` — kick off the one narrated, stock-footage video.
- ``get_workflow_run`` / ``poll_workflow_run`` — follow that production run.
- ``export_project`` / ``get_project_export`` / ``poll_project_export`` — render
  the finished project to a downloadable MP4.

Design notes:

- Credentials and base URL come from Django settings via :meth:`from_settings`;
  nothing is ever hard-coded. ``VIDEOGEN_BASE_URL`` overrides the default host
  verbatim when set.
- Every non-2xx response and every transport failure is raised as a typed
  :class:`~bakerydemo.videos.videogen.exceptions.VideoGenError`, so callers never
  have to inspect ``requests`` internals or raw status codes.
- The client only *reads and starts* work. It never issues a second export or
  any standalone media generation, keeping the billing surface minimal.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

from .exceptions import (
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenError,
    VideoGenJobFailedError,
    VideoGenTimeoutError,
    api_error_for_status,
)

logger = logging.getLogger("bakerydemo.videos.videogen")

DEFAULT_BASE_URL = "https://api.videogen.io"

# Terminal workflow-run / export states as documented by the provider.
TERMINAL_SUCCESS = "succeeded"
TERMINAL_FAILURE_STATES = frozenset({"failed", "cancelled"})
TERMINAL_STATES = frozenset({TERMINAL_SUCCESS, *TERMINAL_FAILURE_STATES})

# Quality tier that maps to a 720p export (per the VideoGen docs).
QUALITY_720P = "STANDARD"

ProgressCallback = Callable[[dict[str, Any]], None]


class VideoGenClient:
    """HTTP client for a single VideoGen team, authenticated by API key."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise VideoGenConfigurationError(
                "No VideoGen API key configured. Set the VIDEOGEN_API_KEY "
                "environment variable."
            )
        self.api_key = api_key
        # Trailing slashes would double up when joined with a leading-slash path.
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._session = session or requests.Session()

    @classmethod
    def from_settings(cls, **overrides: Any) -> "VideoGenClient":
        """Build a client from Django settings.

        Reads ``VIDEOGEN_API_KEY`` (required) and ``VIDEOGEN_BASE_URL``
        (optional override) off the project's settings, which in turn source
        them from the environment. Raises
        :class:`VideoGenConfigurationError` when no API key is available.
        """
        from django.conf import settings

        api_key = getattr(settings, "VIDEOGEN_API_KEY", "") or ""
        base_url = getattr(settings, "VIDEOGEN_BASE_URL", "") or None
        params: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        params.update(overrides)
        return cls(**params)

    # -- Low-level request handling ------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self._session.request(
                method,
                url,
                json=json,
                headers=self._headers(),
                timeout=(self.connect_timeout, self.read_timeout),
            )
        except requests.RequestException as exc:
            raise VideoGenConnectionError(
                f"Could not reach VideoGen at {url}: {exc}"
            ) from exc

        return self._parse_response(response, method=method, url=url)

    def _parse_response(
        self, response: requests.Response, *, method: str, url: str
    ) -> dict[str, Any]:
        # Try to decode a JSON body; VideoGen returns JSON for both success and
        # error responses, but be defensive against empty / non-JSON bodies.
        body: Any
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {"message": response.text}

        if response.ok:
            return body if isinstance(body, dict) else {"data": body}

        message = None
        code = None
        if isinstance(body, dict):
            message = body.get("message")
            code = body.get("code")
        message = message or f"VideoGen request failed ({response.status_code})"

        retry_after = self._parse_retry_after(response)
        logger.warning(
            "VideoGen %s %s -> %s (code=%s): %s",
            method,
            url,
            response.status_code,
            code,
            message,
        )
        raise api_error_for_status(
            response.status_code,
            message,
            code=code,
            body=body,
            retry_after=retry_after,
        )

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> float | None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(0.0, float(reset) - time.time())
            except ValueError:
                pass
        return None

    # -- Workflow: script -> video -------------------------------------------

    def create_script_to_video(
        self,
        script: str,
        *,
        aspect_ratio: tuple[int, int] = (16, 9),
    ) -> dict[str, Any]:
        """Start a stock-footage, voice-narrated video from ``script``.

        Requests the cheapest documented shape: stock footage visuals and a
        narration voice only — no avatars, no image-to-video, no extra media.
        ``aspect_ratio`` is a (width, height) ratio pair sent as the object the
        API expects (e.g. ``(16, 9)``). Returns the provider payload, which
        includes ``workflowRunId`` and ``projectId``.
        """
        width, height = aspect_ratio
        payload = {
            "script": script,
            "visualStyle": {"type": "STOCK"},
            "aspectRatio": {"width": width, "height": height},
        }
        return self._request(
            "POST", "/v1/workflows/script-to-video", json=payload
        )

    def get_workflow_run(self, workflow_run_id: str) -> dict[str, Any]:
        """Fetch the current state of a workflow run."""
        return self._request("GET", f"/v1/workflows/runs/{workflow_run_id}")

    # -- Project export -------------------------------------------------------

    def export_project(
        self,
        project_id: str,
        *,
        quality: str = QUALITY_720P,
    ) -> dict[str, Any]:
        """Start a single export of ``project_id`` at the given quality tier.

        Defaults to ``STANDARD`` (720p). Uses the provider's default watermark
        and end-screen handling so no Pro-only parameters are required.
        """
        payload = {"quality": quality}
        return self._request(
            "POST", f"/v1/projects/{project_id}/export", json=payload
        )

    def get_project_export(
        self, project_id: str, export_id: str
    ) -> dict[str, Any]:
        """Fetch export status; re-signs the download URL when near expiry."""
        return self._request(
            "GET", f"/v1/projects/{project_id}/exports/{export_id}"
        )

    # -- Polling helpers ------------------------------------------------------

    def poll_workflow_run(
        self,
        workflow_run_id: str,
        *,
        interval: float = 3.0,
        timeout: float = 900.0,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Poll a workflow run until it reaches a terminal state.

        Returns the final run payload on ``succeeded``. Raises
        :class:`VideoGenJobFailedError` on ``failed``/``cancelled`` and
        :class:`VideoGenTimeoutError` if ``timeout`` seconds elapse first.
        """
        return self._poll(
            lambda: self.get_workflow_run(workflow_run_id),
            what=f"workflow run {workflow_run_id}",
            interval=interval,
            timeout=timeout,
            on_progress=on_progress,
        )

    def poll_project_export(
        self,
        project_id: str,
        export_id: str,
        *,
        interval: float = 3.0,
        timeout: float = 900.0,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Poll an export until it reaches a terminal state (see above)."""
        return self._poll(
            lambda: self.get_project_export(project_id, export_id),
            what=f"export {export_id}",
            interval=interval,
            timeout=timeout,
            on_progress=on_progress,
        )

    def _poll(
        self,
        fetch: Callable[[], dict[str, Any]],
        *,
        what: str,
        interval: float,
        timeout: float,
        on_progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            payload = fetch()
            status = str(payload.get("status", "")).lower()
            if on_progress is not None:
                on_progress(payload)

            if status == TERMINAL_SUCCESS:
                return payload
            if status in TERMINAL_FAILURE_STATES:
                error = payload.get("error") or {}
                detail = (
                    error.get("message")
                    if isinstance(error, dict)
                    else str(error)
                )
                raise VideoGenJobFailedError(
                    f"VideoGen {what} {status}: {detail or 'no detail provided'}",
                    body=payload,
                )

            if time.monotonic() >= deadline:
                raise VideoGenTimeoutError(
                    f"VideoGen {what} did not finish within {timeout:.0f}s "
                    f"(last status: {status or 'unknown'})",
                    body=payload,
                )
            time.sleep(interval)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<VideoGenClient base_url={self.base_url!r}>"
