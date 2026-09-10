"""A thin, typed HTTP client for the VideoGen REST API.

Only the endpoints this integration needs are implemented, and every one of
them is driven by the ``videogen-docs`` reference:

* ``POST /v1/workflows/script-to-video``            - start production
* ``GET  /v1/workflows/runs/{workflowRunId}``       - poll generation
* ``POST /v1/projects/{projectId}/export``          - start the MP4 export
* ``GET  /v1/projects/{projectId}/exports/{id}``    - poll the export

Provider failures are surfaced as the typed exceptions in
:mod:`bakerydemo.videogen.exceptions`. The base URL defaults to the documented
``https://api.videogen.io`` and is overridden verbatim by ``VIDEOGEN_BASE_URL``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from django.conf import settings

from .exceptions import (
    VideoGenAPIError,
    VideoGenAuthenticationError,
    VideoGenBadRequestError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenNotFoundError,
    VideoGenPermissionError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenTimeoutError,
)

logger = logging.getLogger("bakerydemo.videogen")

DEFAULT_BASE_URL = "https://api.videogen.io"

# VideoGen expresses aspect ratio as a {width, height} ratio object (not a
# string and not pixel dimensions). 16:9 is the documented widescreen default.
ASPECT_RATIO_16_9 = {"width": 16, "height": 9}

# Statuses shared by workflow runs and project exports.
STATUS_SUCCEEDED = "succeeded"
TERMINAL_FAILURE_STATUSES = frozenset({"failed", "cancelled"})

_STATUS_EXCEPTIONS = {
    400: VideoGenBadRequestError,
    401: VideoGenAuthenticationError,
    403: VideoGenPermissionError,
    404: VideoGenNotFoundError,
    429: VideoGenRateLimitError,
}


class VideoGenClient:
    """Minimal synchronous client for the VideoGen API."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise VideoGenConfigurationError("VIDEOGEN_API_KEY is not set.")
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.session = session or requests.Session()

    @classmethod
    def from_settings(cls, **kwargs: Any) -> VideoGenClient:
        """Build a client from Django settings.

        Reads ``VIDEOGEN_API_KEY`` (required) and ``VIDEOGEN_BASE_URL``
        (optional override) from settings, which in turn read them from the
        environment. Raises :class:`VideoGenConfigurationError` if the key is
        absent so callers can distinguish misconfiguration from provider faults.
        """
        api_key = getattr(settings, "VIDEOGEN_API_KEY", "") or ""
        base_url = getattr(settings, "VIDEOGEN_BASE_URL", "") or None
        return cls(api_key, base_url, **kwargs)

    # -- Public endpoints --------------------------------------------------

    def create_script_to_video(
        self,
        *,
        script: str,
        visual_style: dict | None = None,
        aspect_ratio: dict | None = None,
        **extra: Any,
    ) -> dict:
        """Start a script-to-video workflow. Returns the 202 body.

        Defaults encode the cheapest documented shape: stock footage visuals
        (``visualStyle: {type: STOCK}``), a plain narration voice (no actor /
        avatar), and a 16:9 aspect ratio. This POST is never retried, so a
        transient network error can never trigger a second billed production.
        """
        payload: dict[str, Any] = {
            "script": script,
            "visualStyle": visual_style or {"type": "STOCK"},
            "aspectRatio": aspect_ratio or dict(ASPECT_RATIO_16_9),
        }
        payload.update(extra)
        return self._request(
            "POST", "/v1/workflows/script-to-video", json=payload, idempotent=False
        )

    def get_workflow_run(self, workflow_run_id: str) -> dict:
        """Poll a workflow run's status (idempotent read, safe to retry)."""
        return self._request(
            "GET", f"/v1/workflows/runs/{workflow_run_id}", idempotent=True
        )

    def export_project(self, project_id: str, *, quality: str = "STANDARD") -> dict:
        """Start a single MP4 export. Returns the 202 body.

        ``quality="STANDARD"`` maps to a 720p export per the VideoGen docs.
        This POST is never retried to guarantee exactly one export per project.
        """
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/export",
            json={"quality": quality},
            idempotent=False,
        )

    def get_project_export(self, project_id: str, export_id: str) -> dict:
        """Poll an export's status/download URL (idempotent read, safe to retry)."""
        return self._request(
            "GET",
            f"/v1/projects/{project_id}/exports/{export_id}",
            idempotent=True,
        )

    # -- Internals ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        idempotent: bool,
    ) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        # Only idempotent reads are retried; billed POSTs are attempted once.
        attempts = self.max_retries if idempotent else 1

        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    json=json,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.Timeout:
                last_exc = VideoGenTimeoutError(
                    f"Timed out calling VideoGen {method} {path}."
                )
            except requests.RequestException as exc:
                last_exc = VideoGenConnectionError(
                    f"Could not reach VideoGen for {method} {path}: {exc}"
                )
            else:
                if response.status_code < 400:
                    return self._parse_body(response)
                # An error status: retry transient failures on idempotent reads,
                # otherwise raise the appropriate typed exception immediately.
                if self._should_retry_status(response.status_code, idempotent, attempt):
                    self._sleep_before_retry(attempt, response=response)
                    continue
                self._raise_api_error(method, path, response)

            # Transport error path: retry idempotent reads, else give up.
            if attempt < attempts:
                self._sleep_before_retry(attempt)
                continue
            break

        assert last_exc is not None  # only reached after a transport failure
        raise last_exc

    def _should_retry_status(self, status: int, idempotent: bool, attempt: int) -> bool:
        if not idempotent or attempt >= self.max_retries:
            return False
        return status == 429 or status >= 500

    def _raise_api_error(
        self, method: str, path: str, response: requests.Response
    ) -> None:
        status = response.status_code
        message, code, body = self._extract_api_error(response)
        exc_class = _STATUS_EXCEPTIONS.get(status)
        if exc_class is None:
            exc_class = VideoGenServerError if status >= 500 else VideoGenAPIError
        raise exc_class(
            f"VideoGen {method} {path} failed ({status}): {message}",
            status=status,
            code=code,
            body=body,
        )

    @staticmethod
    def _extract_api_error(response: requests.Response) -> tuple[str, str | None, Any]:
        try:
            body = response.json()
        except ValueError:
            return (response.text or response.reason or "Unknown error"), None, response.text
        if isinstance(body, dict):
            message = body.get("message") or body.get("detail") or str(body)
            return message, body.get("code"), body
        return str(body), None, body

    @staticmethod
    def _parse_body(response: requests.Response) -> dict:
        if response.status_code == 204 or not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise VideoGenAPIError(
                "VideoGen returned a non-JSON response.",
                status=response.status_code,
                body=response.text,
            ) from exc
        return data if isinstance(data, dict) else {"data": data}

    def _sleep_before_retry(
        self, attempt: int, response: requests.Response | None = None
    ) -> None:
        delay = self.backoff_factor * (2 ** (attempt - 1))
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
        logger.warning("Retrying VideoGen request in %.1fs (attempt %d)", delay, attempt)
        time.sleep(delay)
