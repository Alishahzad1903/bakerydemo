"""Orchestrate an article-to-video production run.

This module ties the pieces together:

- :func:`start_video_job` is called by the API. It creates (or reuses) the
  single :class:`VideoJob` for a page and, only when it created a new one,
  launches the production pipeline in a background thread so the HTTP request
  can return immediately.
- :func:`run_pipeline` drives the VideoGen calls end to end: start the video,
  follow the run to completion, export once at 720p, then follow the export to
  a downloadable MP4 — updating the job's progress as it goes.
- :func:`refresh_download_url` re-fetches a fresh signed download URL on demand,
  so a finished video stays downloadable for as long as the article exists.

There is deliberately no task queue or broker here: a daemon thread is enough
for this additive, low-volume capability and honours the "no new infra"
constraint. Each background run uses its own database connection and closes it
on the way out.
"""

from __future__ import annotations

import logging
import threading

from django.db import connection, transaction

from .models import VideoJob
from .narration import build_narration_script
from .videogen import VideoGenClient, VideoGenError

logger = logging.getLogger("bakerydemo.videos")

# How progress from each phase maps onto the single 0-100 bar we expose:
# production takes the first 80%, the export the final 20%.
_PRODUCTION_WEIGHT = 0.8


def build_client() -> VideoGenClient:
    """Construct a VideoGen client from Django settings."""
    return VideoGenClient.from_settings()


def start_video_job(page) -> VideoJob:
    """Return the video job for ``page``, creating and starting one if needed.

    Idempotent: a page has at most one :class:`VideoJob`. If one already exists
    (in any state) it is returned as-is and no new production run is started, so
    repeated requests never produce a second video or incur a second charge.
    """
    script = build_narration_script(page.title, getattr(page, "introduction", ""))

    with transaction.atomic():
        job, created = VideoJob.objects.get_or_create(
            page=page,
            defaults={
                "script": script,
                "status": VideoJob.Status.PROCESSING,
                "progress_percentage": 0,
            },
        )

    if created:
        logger.info(
            "Started video job %s for page %s (%d-word script)",
            job.id,
            page.pk,
            len(script.split()),
        )
        launch_pipeline(job.id)
    else:
        logger.info(
            "Reusing existing video job %s for page %s (status=%s)",
            job.id,
            page.pk,
            job.status,
        )

    return job


def launch_pipeline(job_id) -> threading.Thread:
    """Run the production pipeline for ``job_id`` in a daemon thread."""
    thread = threading.Thread(
        target=run_pipeline,
        args=(job_id,),
        name=f"videogen-{job_id}",
        daemon=True,
    )
    thread.start()
    return thread


def _set_progress(job: VideoJob, phase: str, payload: dict) -> None:
    """Persist a progress update from a poll callback."""
    raw = payload.get("progressPercentage")
    try:
        pct = max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return

    if phase == "production":
        combined = round(pct * _PRODUCTION_WEIGHT)
    else:  # export
        combined = round(100 * _PRODUCTION_WEIGHT + pct * (1 - _PRODUCTION_WEIGHT))

    combined = max(0, min(99, combined))  # 100 is reserved for READY
    if combined != job.progress_percentage:
        job.progress_percentage = combined
        job.save(update_fields=["progress_percentage", "updated_at"])


def run_pipeline(job_id, *, client: VideoGenClient | None = None) -> None:
    """Execute the full VideoGen production + export flow for one job.

    Any provider failure is surfaced as a typed :class:`VideoGenError` and
    recorded on the job as a terminal ``failed`` state with its message; the
    exception is not re-raised because this runs on a background thread.
    """
    try:
        job = VideoJob.objects.get(id=job_id)
    except VideoJob.DoesNotExist:
        logger.error("Video job %s vanished before the pipeline started", job_id)
        return

    try:
        client = client or build_client()

        # 1. Start the one video: stock footage, voice narration, 16:9.
        start = client.create_script_to_video(job.script)
        job.workflow_run_id = start.get("workflowRunId", "") or ""
        job.project_id = start.get("projectId", "") or ""
        job.save(update_fields=["workflow_run_id", "project_id", "updated_at"])

        # 2. Follow the production run to completion.
        final_run = client.poll_workflow_run(
            job.workflow_run_id,
            on_progress=lambda p: _set_progress(job, "production", p),
        )
        project_id = final_run.get("projectId") or job.project_id
        if not project_id:
            raise VideoGenError(
                "VideoGen reported success but returned no projectId to export.",
                body=final_run,
            )
        job.project_id = project_id

        # 3. Export exactly once, at 720p.
        export = client.export_project(project_id)
        job.export_id = export.get("exportId", "") or ""
        job.save(update_fields=["project_id", "export_id", "updated_at"])

        # 4. Follow the export to a downloadable MP4.
        final_export = client.poll_project_export(
            project_id,
            job.export_id,
            on_progress=lambda p: _set_progress(job, "export", p),
        )
        download_url = final_export.get("downloadUrl", "") or ""
        if not download_url:
            raise VideoGenError(
                "VideoGen export succeeded but returned no downloadUrl.",
                body=final_export,
            )

        job.download_url = download_url
        job.status = VideoJob.Status.READY
        job.progress_percentage = 100
        job.error = ""
        job.save(
            update_fields=[
                "download_url",
                "status",
                "progress_percentage",
                "error",
                "updated_at",
            ]
        )
        logger.info("Video job %s ready", job.id)

    except VideoGenError as exc:
        _mark_failed(job, str(exc))
    except Exception as exc:  # defensive: never leave a job stuck "processing"
        logger.exception("Unexpected error in video job %s", job.id)
        _mark_failed(job, f"Unexpected error: {exc}")
    finally:
        # Release this thread's database connection.
        connection.close()


def _mark_failed(job: VideoJob, message: str) -> None:
    job.status = VideoJob.Status.FAILED
    job.error = message
    job.save(update_fields=["status", "error", "updated_at"])
    logger.warning("Video job %s failed: %s", job.id, message)


def refresh_download_url(job: VideoJob, *, client: VideoGenClient | None = None) -> VideoJob:
    """Return ``job`` with a freshly signed download URL when it is ready.

    VideoGen download URLs are signed and expire; re-fetching the export status
    yields a re-signed URL. Best-effort: if the refresh call fails, the last
    known URL is kept so the endpoint stays usable.
    """
    if job.status != VideoJob.Status.READY or not (job.project_id and job.export_id):
        return job

    try:
        client = client or build_client()
        export = client.get_project_export(job.project_id, job.export_id)
    except VideoGenError as exc:
        logger.warning(
            "Could not refresh download URL for video job %s: %s", job.id, exc
        )
        return job

    fresh_url = export.get("downloadUrl", "") or ""
    if fresh_url and fresh_url != job.download_url:
        job.download_url = fresh_url
        job.save(update_fields=["download_url", "updated_at"])
    return job
