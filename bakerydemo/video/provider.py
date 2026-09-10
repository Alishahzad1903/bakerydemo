"""VideoGen provider gateway.

A thin, well-typed seam over the VideoGen Python SDK. Every SDK call goes through
:meth:`VideoGenGateway._sdk_call`, which converts the SDK's failure surface
(``ApiError``, ``httpx`` transport errors, and decode failures) into the typed
exceptions in :mod:`bakerydemo.video.exceptions`. Nothing outside this module
imports the SDK.

Design constraints honoured here (from the task brief):

* **Sync** client — the whole Django/Ninja stack is synchronous.
* **Cheap shape only** — script-to-video with **stock** footage and a plain
  voiceover (no avatar/presenter), a single **720p**, **16:9** export. No remix
  actions, no image-to-video, no standalone media generation.
* **Credentials from settings**, never hard-coded.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import httpx
from django.conf import settings
from pydantic import ValidationError
from videogen import VideogenClient
from videogen.core import ApiError, RawError
from videogen.models.enums import ExportProjectQuality, WorkflowVisualStyleType

from .exceptions import (
    VideoGenAPIError,
    VideoGenConfigError,
    VideoGenResponseError,
    VideoGenTransportError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

    from videogen.models import ProjectExport, WorkflowRun
    from videogen.models.aspect_ratio import AspectRatioDict
    from videogen.models.export_project_request import ExportProjectRequestDict
    from videogen.models.script_to_video_request import ScriptToVideoRequestDict

# The VideoGen calls this integration makes all return quickly (they *start* work
# or *poll* a job); 60s is generous headroom over the SDK's 30s default without
# masking a genuinely stuck request. The long waits are our own polling loops.
CLIENT_TIMEOUT = 60.0

# Downloading the finished MP4 from VideoGen's signed URL can take longer than an
# API call, so it gets its own, longer bound.
DOWNLOAD_TIMEOUT = 120.0

# The one cheap, fixed export shape. 720p == the HD tier on VideoGen's standard
# HD ladder (STANDARD/HIGH/FULL_HIGH/ULTRA_HIGH == SD/HD/Full-HD/Ultra-HD-4K).
EXPORT_QUALITY_720P = ExportProjectQuality.HIGH
VISUAL_STYLE_STOCK = WorkflowVisualStyleType.STOCK
ASPECT_RATIO_16_9: AspectRatioDict = {"width": 16, "height": 9}


def build_client() -> VideogenClient:
    """Construct a sync VideoGen client from Django settings.

    Reads ``VIDEOGEN_API_KEY`` (required) and ``VIDEOGEN_BASE_URL`` (optional,
    passed verbatim when set) at call time. Never hard-codes a credential.
    """
    api_key = getattr(settings, "VIDEOGEN_API_KEY", None)
    if not api_key:
        raise VideoGenConfigError(
            "VIDEOGEN_API_KEY is not configured; cannot reach VideoGen."
        )

    base_url = getattr(settings, "VIDEOGEN_BASE_URL", None) or None
    if base_url:
        return VideogenClient(
            bearer_auth=api_key, base_url=base_url, timeout=CLIENT_TIMEOUT
        )
    return VideogenClient(bearer_auth=api_key, timeout=CLIENT_TIMEOUT)


class VideoGenGateway:
    """Typed façade over the VideoGen SDK for the article-video flow.

    Owns a long-lived (per-job) client and must be closed; use it as a context
    manager. A client may be injected for testing.
    """

    def __init__(self, client: VideogenClient | None = None) -> None:
        self._client = client if client is not None else build_client()

    def __enter__(self) -> VideoGenGateway:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- SDK failure translation --------------------------------------------

    @contextmanager
    def _sdk_call(self, operation: str) -> Iterator[None]:
        """Translate SDK failures into typed integration exceptions.

        Order matters: ``ApiError`` (a provider rejection) first, then transport
        errors, then decode failures. ``ValidationError`` is a ``ValueError``, so
        the final clause covers both a non-JSON body and a schema mismatch — both
        mean "the response was unreadable", which bypasses the SDK's own error
        handling by design.
        """
        try:
            yield
        except ApiError as exc:
            raise self._as_api_error(exc) from exc
        except httpx.HTTPError as exc:
            raise VideoGenTransportError(
                f"VideoGen {operation} could not be reached: {exc}"
            ) from exc
        except (ValidationError, ValueError) as exc:
            raise VideoGenResponseError(
                f"VideoGen {operation} returned an unreadable response: {exc}"
            ) from exc

    @staticmethod
    def _as_api_error(exc: ApiError) -> VideoGenAPIError:
        """Build a :class:`VideoGenAPIError` from the SDK's ``ApiError``.

        Every operation in this SDK is "Case B": ``exc.error`` is a ``RawError``.
        VideoGen's error body is a JSON object with ``message``/``code`` when it
        sends one; fall back to the raw text otherwise.
        """
        raw = exc.error
        message: str | None = None
        code: str | None = None
        if isinstance(raw, RawError):
            try:
                body = raw.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                raw_message = body.get("message")
                raw_code = body.get("code")
                message = raw_message if isinstance(raw_message, str) else None
                code = raw_code if isinstance(raw_code, str) else None
            if not message:
                text = (raw.text() or "").strip()
                message = text[:500] or None
        if not message:
            message = f"VideoGen returned HTTP {exc.status_code}"
        return VideoGenAPIError(message, status_code=exc.status_code, code=code)

    # -- Operations ----------------------------------------------------------

    def start_script_to_video(self, script: str) -> tuple[str, str]:
        """Start a script-to-video workflow. Returns ``(workflow_run_id, project_id)``.

        The narration ``script`` is used verbatim. Only the cheap shape is
        requested: stock footage visuals and a plain voiceover (no avatar), 16:9.
        """
        body: ScriptToVideoRequestDict = {
            "script": script,
            "visual_style": {"type_": VISUAL_STYLE_STOCK},
            "aspect_ratio": ASPECT_RATIO_16_9,
        }
        with self._sdk_call("script_to_video"):
            response = self._client.workflows.script_to_video(body=body)
        return response.workflow_run_id, response.project_id

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        """Fetch the current state of a workflow run."""
        with self._sdk_call("get_workflow_run"):
            return self._client.workflows.get_workflow_run(workflow_run_id)

    def export_project(self, project_id: str) -> str:
        """Start a single 720p export of ``project_id``. Returns the export id.

        Watermark/end-screen are left at their defaults (AUTO): removing them
        requires a Pro plan and would error. Called at most once per project.
        """
        body: ExportProjectRequestDict = {"quality": EXPORT_QUALITY_720P}
        with self._sdk_call("export_project"):
            response = self._client.projects.export_project(project_id, body=body)
        return response.export_id

    def get_project_export(self, project_id: str, export_id: str) -> ProjectExport:
        """Fetch the current state of a project export."""
        with self._sdk_call("get_project_export"):
            return self._client.projects.get_project_export(project_id, export_id)

    def download_mp4(self, url: str) -> bytes:
        """Download the finished MP4 from VideoGen's signed URL."""
        try:
            with httpx.Client(
                timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as http_client:
                response = http_client.get(url)
                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as exc:
            raise VideoGenTransportError(
                f"downloading the MP4 failed: HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise VideoGenTransportError(f"downloading the MP4 failed: {exc}") from exc
