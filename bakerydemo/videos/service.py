"""Orchestration for producing an article's video.

This module owns the whole flow and every provider-mutating call, so the spend
guarantees live in one place:

* **Exactly one billable video per article.** :func:`request_video` is
  idempotent - a repeated request for the same page returns the existing active
  job instead of starting another. The workflow-create and export calls each run
  at most once per job (guarded on the stored provider ids).
* **The cheap shape only.** Stock footage, a voice-only narration, one 720p
  export at 16:9. No image-to-video, upscaling, avatars, extra media or second
  export is ever requested.

The pipeline runs on a background thread (the site has no task queue, and the
brief forbids adding one). Reads are decoupled: :func:`get_job` returns the
stored state and :func:`refresh_download_url` re-signs the MP4 link on demand,
so the finished video stays downloadable for as long as the article exists.
"""

from __future__ import annotations

import logging
import threading
import time

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from .models import VideoJob
from .narration import build_narration_script
from .videogen import VideoGenError, get_client
from .videogen.client import TERMINAL_STATUSES, VideoGenClient
from .videogen.exceptions import (
    VideoGenExportFailedError,
    VideoGenWorkflowFailedError,
)

logger = logging.getLogger("bakerydemo.videos")

# --- The cheap shape (fixed by the spend mandate, not a runtime option) ----
VISUAL_STYLE = {"type": "STOCK"}  # stock footage only, never AI imagery
# aspectRatio is a ratio *object* ({width, height}), per VideoGen's schema
# reference and confirmed by the API's own request validation - not the "16:9"
# string that some prose examples imply. 16:9 keeps the widescreen shape and
# means the output is never resized afterwards.
ASPECT_RATIO = {"width": 16, "height": 9}
# Export resolution tier. VideoGen's export `quality` enum is
# STANDARD / HIGH / FULL_HIGH / ULTRA_HIGH (per the MCP docs), NOT the "720p"
# string some prose examples show (which the API rejects). STANDARD is the
# 720p tier; HIGH is the 1080p default and ULTRA_HIGH is 4K, which the spend
# mandate forbids. We export exactly once, at STANDARD (720p).
EXPORT_QUALITY = "STANDARD"

# --- Polling budget --------------------------------------------------------
POLL_INTERVAL_SECONDS = 3.0
PHASE_TIMEOUT_SECONDS = 900.0  # 15 minutes per phase (generation, export)


class VideoJobNotFound(Exception):
    """Raised when a (page, video_job_id) pair does not resolve to a job."""


def request_video(page) -> VideoJob:
    """Return an active :class:`VideoJob` for ``page``, starting one if needed.

    Idempotent by design: if a pending/processing/ready job already exists for
    the page it is returned unchanged (no new provider work, no new charge).
    Only when there is no active job is a new one created and its pipeline
    kicked off on a background thread.
    """
    script = build_narration_script(
        title=page.title,
        introduction=getattr(page, "introduction", "") or "",
    )

    created_job: VideoJob | None = None
    with transaction.atomic():
        existing = (
            VideoJob.objects.select_for_update()
            .filter(page=page, status__in=VideoJob.ACTIVE_STATUSES)
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return existing
        try:
            created_job = VideoJob.objects.create(
                page=page,
                status=VideoJob.Status.PENDING,
                script=script,
            )
        except IntegrityError:
            # A concurrent request won the race and created the active job
            # first; fall back to returning that one.
            created_job = None

    if created_job is None:
        return (
            VideoJob.objects.filter(page=page, status__in=VideoJob.ACTIVE_STATUSES)
            .order_by("-created_at")
            .first()
        )

    _start_pipeline(created_job.id)
    return created_job


def get_job(page, video_job_id) -> VideoJob:
    """Fetch the job identified by ``video_job_id`` scoped to ``page``.

    Scoping to the page means a job id from another article cannot be read via
    the wrong page's URL. Raises :class:`VideoJobNotFound` otherwise.
    """
    try:
        return VideoJob.objects.get(pk=video_job_id, page=page)
    except (VideoJob.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        # ValidationError/ValueError/TypeError guard a malformed (non-UUID) id.
        raise VideoJobNotFound(str(video_job_id)) from exc


def refresh_download_url(
    job: VideoJob, *, client: VideoGenClient | None = None
) -> VideoJob:
    """Re-sign the MP4 link for a ready job (best-effort, read-only).

    VideoGen's export endpoint re-signs the download URL when it nears expiry,
    so re-fetching it is how the site keeps the finished video reachable well
    beyond the initial signed-URL lifetime. This never starts a new export and
    never raises: a transient provider blip must not break a status read, so the
    previously stored URL is kept on failure.
    """
    if job.status != VideoJob.Status.READY or not (job.project_id and job.export_id):
        return job
    try:
        client = client or get_client()
        export = client.get_project_export(job.project_id, job.export_id)
    except VideoGenError as exc:
        logger.warning("Could not refresh download URL for job %s: %s", job.id, exc)
        return job
    download_url = export.get("downloadUrl")
    if download_url and download_url != job.download_url:
        job.download_url = download_url
        job.save(update_fields=["download_url", "updated_at"])
    return job


# --- Background pipeline ----------------------------------------------------


def _start_pipeline(job_id) -> None:
    thread = threading.Thread(
        target=run_pipeline,
        args=(job_id,),
        name=f"videojob-{job_id}",
        daemon=True,
    )
    thread.start()


def run_pipeline(job_id, *, client: VideoGenClient | None = None) -> None:
    """Drive a job from PENDING to READY (or FAILED).

    Safe to call directly (e.g. synchronously in tests). Any provider failure is
    caught and recorded on the job as a typed-exception message, so it surfaces
    through the status endpoint's ``error`` field rather than crashing a thread.
    """
    try:
        client = client or get_client()
        job = VideoJob.objects.get(pk=job_id)
        _execute(job, client)
    except VideoGenError as exc:
        logger.exception("Video job %s failed: %s", job_id, exc)
        _fail(job_id, str(exc))
    except Exception as exc:
        logger.exception("Video job %s failed unexpectedly: %s", job_id, exc)
        _fail(job_id, f"Unexpected error: {exc}")
    finally:
        # Return the thread's DB connection to the pool.
        connection.close()


def _execute(job: VideoJob, client: VideoGenClient) -> None:
    # 1. Start the workflow exactly once.
    if not job.workflow_run_id:
        result = client.create_script_to_video(
            script=job.script,
            visual_style=VISUAL_STYLE,
            aspect_ratio=ASPECT_RATIO,
        )
        job.workflow_run_id = result.get("workflowRunId", "")
        job.project_id = result.get("projectId", "")
        job.status = VideoJob.Status.PROCESSING
        job.save(
            update_fields=["workflow_run_id", "project_id", "status", "updated_at"]
        )
        if not job.workflow_run_id or not job.project_id:
            raise VideoGenError(
                "VideoGen did not return a workflowRunId/projectId for the run."
            )

    # 2. Poll the run to a terminal state.
    run = _poll(
        lambda: client.get_workflow_run(job.workflow_run_id),
        on_progress=lambda pct: _set_progress(job, _scale_generation(pct)),
    )
    status = run.get("status")
    if status != "succeeded":
        raise VideoGenWorkflowFailedError(
            _terminal_message("Workflow run", status, run.get("error")),
            code=_error_code(run.get("error")),
        )
    # projectId is authoritative from the run result once available.
    project_id = run.get("projectId") or job.project_id
    if project_id != job.project_id:
        job.project_id = project_id
        job.save(update_fields=["project_id", "updated_at"])

    # 3. Export to MP4 exactly once (720p).
    if not job.export_id:
        export_start = client.export_project(job.project_id, quality=EXPORT_QUALITY)
        job.export_id = export_start.get("exportId", "")
        job.save(update_fields=["export_id", "updated_at"])
        if not job.export_id:
            raise VideoGenError("VideoGen did not return an exportId for the export.")

    # 4. Poll the export to a terminal state.
    export = _poll(
        lambda: client.get_project_export(job.project_id, job.export_id),
        on_progress=lambda pct: _set_progress(job, _scale_export(pct)),
    )
    status = export.get("status")
    if status != "succeeded":
        raise VideoGenExportFailedError(
            _terminal_message("Export", status, export.get("error")),
            code=_error_code(export.get("error")),
        )
    download_url = export.get("downloadUrl")
    if not download_url:
        raise VideoGenExportFailedError(
            "Export succeeded but VideoGen returned no downloadUrl."
        )

    job.download_url = download_url
    job.progress_percentage = 100
    job.status = VideoJob.Status.READY
    job.error = ""
    job.save(
        update_fields=[
            "download_url",
            "progress_percentage",
            "status",
            "error",
            "updated_at",
        ]
    )


def _poll(fetch, *, on_progress) -> dict:
    """Poll ``fetch`` until it reports a terminal status or the budget expires."""
    deadline = time.monotonic() + PHASE_TIMEOUT_SECONDS
    while True:
        payload = fetch()
        progress = payload.get("progressPercentage")
        if isinstance(progress, (int, float)):
            on_progress(int(progress))
        if payload.get("status") in TERMINAL_STATUSES:
            return payload
        if time.monotonic() >= deadline:
            raise VideoGenError(
                "Timed out waiting for VideoGen to finish (exceeded "
                f"{int(PHASE_TIMEOUT_SECONDS)}s)."
            )
        time.sleep(POLL_INTERVAL_SECONDS)


# --- Small helpers ----------------------------------------------------------


def _fail(job_id, message: str) -> None:
    """Record a terminal failure on the job, surfaced via the status endpoint."""
    VideoJob.objects.filter(pk=job_id).update(
        status=VideoJob.Status.FAILED, error=message
    )


def _set_progress(job: VideoJob, pct: int) -> None:
    pct = max(0, min(100, pct))
    if pct != job.progress_percentage:
        job.progress_percentage = pct
        job.save(update_fields=["progress_percentage", "updated_at"])


def _scale_generation(pct: int) -> int:
    # Generation occupies the first 80% of the overall progress bar.
    return min(80, round(pct * 0.8))


def _scale_export(pct: int) -> int:
    # Export occupies the final 20%.
    return 80 + min(20, round(pct * 0.2))


def _terminal_message(what: str, status, error) -> str:
    detail = _error_message(error)
    base = f"{what} ended with status '{status}'."
    return f"{base} {detail}".strip() if detail else base


def _error_message(error) -> str:
    if isinstance(error, dict):
        return error.get("message") or ""
    if isinstance(error, str):
        return error
    return ""


def _error_code(error):
    if isinstance(error, dict):
        return error.get("code")
    return None
