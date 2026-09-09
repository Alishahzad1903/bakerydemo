"""Integration layer over the VideoGen (APIMatic) Python SDK.

This module is the *only* place the site talks to VideoGen. It builds the
client from Django settings, drives the two-phase production flow, and — most
importantly — translates every failure kind the SDK can raise into the typed
exceptions in :mod:`bakerydemo.videos.exceptions`, so no raw SDK, pydantic or
httpx error leaks into the rest of the site.

Production flow (all asynchronous, polled — the SDK performs no retries and no
polling of its own, so both are built here with a bounded deadline):

1. ``workflows.script_to_video`` — builds a project from the narration script,
   using **stock footage** and a **voiceover only** (no avatar/presenter),
   at a **16:9** aspect ratio.
2. Poll ``workflows.get_workflow_run`` until the build is terminal.
3. ``projects.export_project`` — renders the project to MP4 **once**, at
   **720p** (``HIGH`` tier; see ``videogen-plan.md`` for the tier mapping).
4. Poll ``projects.get_project_export`` until terminal, then return the signed
   MP4 download URL.

The cheap, fixed request shape (stock footage, one voice, one 720p export,
16:9, no remix actions) is a hard spending requirement, not a preference.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from django.conf import settings
from pydantic import ValidationError
from videogen import VideogenClient
from videogen.core import ApiError, RawError
from videogen.models import (
    ApiErrorModel,
    ExportProjectRequestDict,
    ScriptToVideoRequestDict,
)
from videogen.models.enums.export_project_quality import ExportProjectQuality
from videogen.models.enums.job_status import JobStatus
from videogen.models.enums.workflow_visual_style_type import WorkflowVisualStyleType

from .exceptions import (
    VideoGenAPIError,
    VideoGenConfigError,
    VideoGenProductionFailed,
    VideoGenTimeout,
    VideoGenUnavailable,
    VideoGenUnreadableResponse,
)

logger = logging.getLogger("bakerydemo.videos")

# 720p vertical resolution tier. See videogen-plan.md: the SDK exposes
# STANDARD/HIGH/FULL_HIGH/ULTRA_HIGH ("vertical resolution tier"); mapped onto
# the conventional SD/HD/FHD/UHD ladder, HIGH is 720p and ULTRA_HIGH is the
# forbidden 4K.
EXPORT_QUALITY_720P = ExportProjectQuality.HIGH

_TERMINAL_OK = str(JobStatus.SUCCEEDED)
_TERMINAL_FAIL = {str(JobStatus.FAILED), str(JobStatus.CANCELLED)}

_ERROR_DETAIL_MAX = 500


class ProductionHooks(Protocol):
    """Called as production advances so the caller can persist progress."""

    def workflow_started(self, workflow_run_id: str, project_id: str) -> None: ...

    def export_started(self, export_id: str) -> None: ...

    def progress(self, percentage: int) -> None: ...


class NullHooks:
    def workflow_started(self, workflow_run_id: str, project_id: str) -> None: ...

    def export_started(self, export_id: str) -> None: ...

    def progress(self, percentage: int) -> None: ...


@dataclass(frozen=True)
class _StartInfo:
    workflow_run_id: str
    project_id: str


# --- Client construction ---------------------------------------------------


def build_client(timeout: float | None = None) -> VideogenClient:
    """Build a VideoGen client from Django settings.

    Reads the API key (and optional base-URL override) from settings, which in
    turn read them from the environment. Never hard-codes a credential.
    """
    api_key = settings.VIDEOGEN_API_KEY
    if not api_key:
        raise VideoGenConfigError(
            "VIDEOGEN_API_KEY is not set; cannot produce a video."
        )

    resolved_timeout = (
        timeout if timeout is not None else settings.VIDEOGEN_REQUEST_TIMEOUT
    )
    # Use the override verbatim when set; otherwise the SDK default host applies.
    base_url = settings.VIDEOGEN_BASE_URL
    if base_url:
        return VideogenClient(
            bearer_auth=api_key, base_url=base_url, timeout=resolved_timeout
        )
    return VideogenClient(bearer_auth=api_key, timeout=resolved_timeout)


# --- Error translation ------------------------------------------------------


def _error_detail(e: ApiError) -> str:
    """Extract a safe, human-readable detail from a provider error body."""
    # Case B for every operation in scope: ``ApiError.error`` is always RawError.
    raw = cast(RawError, e.error)
    body: object
    try:
        body = raw.json()
    except ValueError:
        body = None
    detail: str
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        detail = body["message"]
        code = body.get("code")
        if isinstance(code, str) and code:
            detail = f"{detail} ({code})"
    else:
        detail = raw.text()
    return detail[:_ERROR_DETAIL_MAX]


def _call[T](operation: str, fn: Callable[[], T]) -> T:
    """Run one SDK call, translating every failure kind into a typed exception."""
    try:
        return fn()
    except ApiError as e:
        detail = _error_detail(e)
        logger.warning("VideoGen %s failed: HTTP %s %s", operation, e.status_code, detail)
        raise VideoGenAPIError(
            f"VideoGen {operation} failed.", status_code=e.status_code, detail=detail
        ) from e
    except ValidationError as e:
        # A decode failure bypasses the SDK's error handling; the outcome is
        # unknown (the request may have taken effect server-side).
        raise VideoGenUnreadableResponse(
            f"VideoGen {operation} returned an unreadable response."
        ) from e
    except ValueError as e:
        # Non-JSON body decode failure.
        raise VideoGenUnreadableResponse(
            f"VideoGen {operation} returned an unreadable response."
        ) from e
    except httpx.HTTPError as e:
        raise VideoGenUnavailable(
            f"VideoGen {operation} could not be reached."
        ) from e


def _job_error_message(err: ApiErrorModel | None) -> str:
    if err is None:
        return ""
    message = getattr(err, "message", "") or ""
    code = getattr(err, "code", None)
    if isinstance(code, str) and code:
        return f"{message} ({code})" if message else code
    return message


# --- SDK operations ---------------------------------------------------------


def _start_script_to_video(client: VideogenClient, script: str) -> _StartInfo:
    # Cheapest shape: stock footage (no AI imagery), voiceover only (no avatar
    # -> actorEntityId omitted), default voice, 16:9, no remix actions.
    body: ScriptToVideoRequestDict = {
        "script": script,
        "visual_style": {"type_": WorkflowVisualStyleType.STOCK},
        "aspect_ratio": {"width": 16, "height": 9},
    }
    resp = _call("script_to_video", lambda: client.workflows.script_to_video(body=body))
    workflow_run_id = resp.workflow_run_id
    project_id = resp.project_id
    if not workflow_run_id or not project_id:
        raise VideoGenUnreadableResponse(
            "VideoGen script_to_video returned no workflow run / project id."
        )
    return _StartInfo(workflow_run_id=workflow_run_id, project_id=project_id)


def _start_export(client: VideogenClient, project_id: str) -> str:
    # Export ONCE, at 720p. Watermark/end-screen left at their defaults (AUTO):
    # forcing them off (NONE) requires a Pro plan and would error.
    body: ExportProjectRequestDict = {"quality": EXPORT_QUALITY_720P}
    resp = _call(
        "export_project",
        lambda: client.projects.export_project(project_id, body=body),
    )
    if not resp.export_id:
        raise VideoGenUnreadableResponse("VideoGen export_project returned no export id.")
    return resp.export_id


def _scale(percentage: float, low: int, high: int) -> int:
    pct = max(0.0, min(100.0, float(percentage)))
    return int(low + (high - low) * pct / 100.0)


def _poll(
    getter: Callable[[], Any],
    *,
    what: str,
    deadline: float,
    poll_interval: float,
    on_progress: Callable[[float], None],
) -> Any:
    """Poll ``getter`` (returning a job model with ``status`` /
    ``progress_percentage`` / ``error``) until it reaches a terminal state.

    Returns the terminal job model on success; raises
    :class:`VideoGenProductionFailed` on a failed/cancelled job, or
    :class:`VideoGenTimeout` if the deadline passes first.
    """
    while True:
        job = getter()
        status_value = str(job.status)
        on_progress(float(job.progress_percentage or 0))

        if status_value == _TERMINAL_OK:
            return job
        if status_value in _TERMINAL_FAIL:
            message = _job_error_message(job.error) or f"VideoGen {what} {status_value}."
            raise VideoGenProductionFailed(message)

        if time.monotonic() >= deadline:
            raise VideoGenTimeout(
                f"VideoGen {what} did not finish within the allotted time."
            )
        time.sleep(poll_interval)


# --- Public entry point -----------------------------------------------------


def produce_video(
    script: str,
    *,
    hooks: ProductionHooks | None = None,
    client: VideogenClient | None = None,
    poll_interval: float | None = None,
    total_timeout: float | None = None,
) -> str:
    """Produce one video from ``script`` and return the signed MP4 download URL.

    Owns the client's lifetime unless one is supplied (tests inject a client
    backed by a fake transport). Raises a :class:`VideoGenError` subclass on
    any provider failure.
    """
    hooks = hooks or NullHooks()
    poll_interval = (
        poll_interval if poll_interval is not None else settings.VIDEOGEN_POLL_INTERVAL
    )
    total_timeout = (
        total_timeout
        if total_timeout is not None
        else settings.VIDEOGEN_PRODUCTION_TIMEOUT
    )
    deadline = time.monotonic() + total_timeout

    owns_client = client is None
    client = client or build_client()
    try:
        # 1. Start the build.
        start = _start_script_to_video(client, script)
        hooks.workflow_started(start.workflow_run_id, start.project_id)
        hooks.progress(5)
        logger.info(
            "VideoGen build started: workflow_run=%s project=%s",
            start.workflow_run_id,
            start.project_id,
        )

        # 2. Wait for the build to finish (progress band 5-50%).
        _poll(
            lambda: _call(
                "get_workflow_run",
                lambda: client.workflows.get_workflow_run(start.workflow_run_id),
            ),
            what="workflow run",
            deadline=deadline,
            poll_interval=poll_interval,
            on_progress=lambda p: hooks.progress(_scale(p, 5, 50)),
        )
        hooks.progress(52)

        # 3. Export once, at 720p.
        export_id = _start_export(client, start.project_id)
        hooks.export_started(export_id)
        logger.info("VideoGen export started: export=%s", export_id)

        # 4. Wait for the export to finish (progress band 55-99%).
        export = _poll(
            lambda: _call(
                "get_project_export",
                lambda: client.projects.get_project_export(
                    start.project_id, export_id
                ),
            ),
            what="export",
            deadline=deadline,
            poll_interval=poll_interval,
            on_progress=lambda p: hooks.progress(_scale(p, 55, 99)),
        )

        download_url: str | None = export.download_url
        if not download_url:
            raise VideoGenUnreadableResponse(
                "VideoGen export succeeded but returned no download URL."
            )
        logger.info("VideoGen export ready: export=%s", export_id)
        return download_url
    finally:
        if owns_client:
            client.close()
