"""
Orchestration for turning a blog article into a downloadable MP4.

The public entry points are:

* :func:`start_or_get_job` — idempotently create (or reuse) a job for a page.
* :func:`enqueue_job` — run a job's production pipeline, in a background thread
  by default (no broker/queue infrastructure required).
* :func:`run_job` — run the full pipeline synchronously (used by the management
  command and by tests).
* :func:`maybe_refresh_download_url` — re-sign an expiring download URL so the
  MP4 stays retrievable for as long as the article exists.
"""

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .client import VideoGenClient
from .exceptions import VideoGenError, VideoGenProductionError, VideoGenTimeoutError
from .models import VideoJob
from .narration import build_narration

logger = logging.getLogger("bakerydemo.video")


def parse_aspect_ratio(raw):
    """
    Normalise an aspect-ratio setting into VideoGen's ``{width, height}`` object.

    Accepts a ``"W:H"`` string (e.g. ``"16:9"``) or an object. Returns ``None``
    for empty/invalid input so the request omits the field and VideoGen applies
    its default (16:9).
    """
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        width, height = str(raw).split(":")
        return {"width": int(width), "height": int(height)}
    except (ValueError, AttributeError):
        return None


_WORKFLOW_OK = {"succeeded"}
_WORKFLOW_BAD = {"failed", "cancelled"}
_EXPORT_OK = {"succeeded"}
_EXPORT_BAD = {"failed", "cancelled"}


# ---------------------------------------------------------------------- #
# Creating / reusing jobs (idempotency)
# ---------------------------------------------------------------------- #
def start_or_get_job(page, user=None):
    """
    Return ``(job, created)`` for ``page``.

    If an active job (pending/processing/ready) already exists it is returned
    with ``created=False`` and no new VideoGen work is started — this is what
    stops a repeated request from producing (and being billed for) a second
    video. A unique constraint guards against a concurrent duplicate.
    """
    narration = build_narration(page)
    try:
        with transaction.atomic():
            existing = _active_job_for_page(page)
            if existing is not None:
                return existing, False
            job = VideoJob.objects.create(page=page, narration=narration)
            return job, True
    except IntegrityError:
        # A concurrent request won the race and created the active job.
        existing = _active_job_for_page(page)
        if existing is not None:
            return existing, False
        raise


def _active_job_for_page(page):
    return (
        VideoJob.objects.filter(page=page, status__in=VideoJob.ACTIVE_STATUSES)
        .order_by("-created_at")
        .first()
    )


# ---------------------------------------------------------------------- #
# Running jobs
# ---------------------------------------------------------------------- #
def enqueue_job(job_id):
    """
    Kick off production for ``job_id``. Runs in a daemon thread so the HTTP
    request can return immediately, unless ``VIDEOGEN_EXECUTE_INLINE`` is set
    (used by tests) in which case it runs synchronously.
    """
    if getattr(settings, "VIDEOGEN_EXECUTE_INLINE", False):
        run_job(job_id)
        return
    thread = threading.Thread(
        target=_threaded_run, args=(job_id,), name=f"videojob-{job_id}", daemon=True
    )
    thread.start()


def _threaded_run(job_id):
    try:
        run_job(job_id)
    except Exception:  # pragma: no cover - defensive; run_job handles its own errors
        logger.exception("Video job %s crashed", job_id)
    finally:
        # Release the thread's database connection.
        connection.close()


def run_job(job_id, client=None):
    """Run the full production pipeline for a pending job."""
    try:
        job = VideoJob.objects.get(pk=job_id)
    except VideoJob.DoesNotExist:
        logger.warning("Video job %s no longer exists", job_id)
        return

    # Only a freshly-pending job may start producing. This guards against a
    # duplicate worker ever re-triggering a billable workflow.
    if job.status != VideoJob.Status.PENDING:
        logger.info("Video job %s already %s; not re-running", job_id, job.status)
        return

    job.mark_processing()
    client = client or VideoGenClient.from_settings()
    try:
        _produce(job, client)
    except VideoGenError as exc:
        logger.warning("Video job %s failed: %s", job_id, exc)
        job.mark_failed(str(exc))
    except Exception as exc:  # pragma: no cover - unexpected, surfaced to caller
        logger.exception("Video job %s unexpected error", job_id)
        job.mark_failed(f"Unexpected error while producing video: {exc}")


def _produce(job, client):
    # 1. Start the script-to-video workflow (stock footage, voice only).
    if not job.workflow_run_id:
        result = client.create_script_to_video(
            script=job.narration,
            visual_style={"type": "STOCK"},
            voice_id=getattr(settings, "VIDEOGEN_VOICE_ID", None) or None,
            aspect_ratio=parse_aspect_ratio(
                getattr(settings, "VIDEOGEN_ASPECT_RATIO", "16:9")
            ),
        )
        job.workflow_run_id = result.get("workflowRunId", "")
        job.project_id = result.get("projectId", "") or job.project_id
        if not job.workflow_run_id:
            raise VideoGenProductionError("VideoGen did not return a workflowRunId.")
        job.save(update_fields=["workflow_run_id", "project_id", "updated_at"])

    # 2. Wait for the workflow to finish (first half of the progress bar).
    run = _poll(
        lambda: client.get_workflow_run(job.workflow_run_id),
        ok=_WORKFLOW_OK,
        bad=_WORKFLOW_BAD,
        on_progress=lambda pct: job.set_progress(int(pct * 0.5)),
        what="workflow run",
    )
    project_id = run.get("projectId") or job.project_id
    if not project_id:
        raise VideoGenProductionError(
            "VideoGen workflow succeeded but returned no projectId."
        )
    if project_id != job.project_id:
        job.project_id = project_id
        job.save(update_fields=["project_id", "updated_at"])
    job.set_progress(50)

    # 3. Export the project to MP4 (quality capped at 1080p or below).
    if not job.export_id:
        export = client.create_export(
            project_id,
            quality=getattr(settings, "VIDEOGEN_EXPORT_QUALITY", "STANDARD"),
        )
        job.export_id = export.get("exportId", "")
        if not job.export_id:
            raise VideoGenProductionError("VideoGen did not return an exportId.")
        job.save(update_fields=["export_id", "updated_at"])

    # 4. Wait for the export to finish (second half of the progress bar).
    export_result = _poll(
        lambda: client.get_export(project_id, job.export_id),
        ok=_EXPORT_OK,
        bad=_EXPORT_BAD,
        on_progress=lambda pct: job.set_progress(50 + int(pct * 0.5)),
        what="export",
    )
    _apply_export(job, export_result)
    if not job.download_url:
        raise VideoGenProductionError(
            "VideoGen export succeeded but returned no downloadUrl."
        )
    job.mark_ready()


def _poll(fetch, *, ok, bad, on_progress, what):
    """Poll ``fetch`` until a terminal status, honouring interval and timeout."""
    interval = getattr(settings, "VIDEOGEN_POLL_INTERVAL", 5)
    timeout = getattr(settings, "VIDEOGEN_POLL_TIMEOUT", 1800)
    deadline = time.monotonic() + timeout
    while True:
        data = fetch()
        status = (data.get("status") or "").lower()
        progress = data.get("progressPercentage")
        if isinstance(progress, (int, float)):
            on_progress(progress)
        if status in ok:
            return data
        if status in bad:
            raise VideoGenProductionError(
                _error_message(data) or f"VideoGen {what} {status}."
            )
        if time.monotonic() >= deadline:
            raise VideoGenTimeoutError(
                f"Timed out waiting for VideoGen {what} to finish."
            )
        time.sleep(interval)


def _error_message(data):
    error = data.get("error")
    if isinstance(error, dict):
        return error.get("message")
    if isinstance(error, str):
        return error
    return None


# ---------------------------------------------------------------------- #
# Keeping the download URL fresh
# ---------------------------------------------------------------------- #
def maybe_refresh_download_url(job, client=None):
    """
    Re-sign the export's download URL when it is missing or close to expiry, so
    the MP4 stays retrievable for as long as the article exists. A refresh
    failure is logged and the existing URL is kept.
    """
    if job.status != VideoJob.Status.READY:
        return job
    if not (job.project_id and job.export_id):
        return job
    if not _needs_refresh(job):
        return job

    client = client or VideoGenClient.from_settings()
    try:
        export_result = client.get_export(job.project_id, job.export_id)
    except VideoGenError as exc:
        logger.warning("Could not refresh download URL for job %s: %s", job.pk, exc)
        return job
    _apply_export(job, export_result)
    return job


def _needs_refresh(job):
    if not job.download_url:
        return True
    if job.download_url_expires_at is None:
        return False
    buffer = timedelta(
        seconds=getattr(settings, "VIDEOGEN_DOWNLOAD_URL_REFRESH_BUFFER", 3600)
    )
    return timezone.now() >= (job.download_url_expires_at - buffer)


def _apply_export(job, export_result):
    job.download_url = export_result.get("downloadUrl", "") or ""
    job.export_file_id = export_result.get("exportFileId", "") or job.export_file_id
    job.download_url_expires_at = _epoch_to_datetime(
        export_result.get("downloadUrlExpiresAt")
    )
    job.save(
        update_fields=[
            "download_url",
            "export_file_id",
            "download_url_expires_at",
            "updated_at",
        ]
    )


def _epoch_to_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None
