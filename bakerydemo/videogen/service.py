"""Orchestrates turning an article into a downloadable video.

The public entry point is :func:`request_video`, which is idempotent per
article: asking twice in a row returns the same job (and never starts a second,
billable production). The heavy lifting runs in a background worker so the HTTP
request can return immediately with a job id.

There is deliberately no broker / task queue here (the environment has none):
the worker is a daemon thread that persists all of its state to the ``VideoJob``
row, so progress and the finished MP4 survive the request that started them.
For a multi-process deployment this thread executor is the single seam to swap
for Celery / Django Tasks — the production logic in :func:`run_job` stays put.
"""

from __future__ import annotations

import logging
import tempfile
import threading

from django.core.files import File
from django.db import IntegrityError, connection, transaction

from .exceptions import VideoGenIntegrationError
from .models import ACTIVE_STATUSES, JobStatus, VideoJob
from .narration import build_narration_script
from .provider import VideoGenProvider

logger = logging.getLogger("bakerydemo.videogen")

# Progress milestones (percent of the overall job).
_PROGRESS_START = 5
_RUN_PROGRESS_FLOOR = 10
_RUN_PROGRESS_CEILING = 70
_PROGRESS_EXPORT = 90
_PROGRESS_DOWNLOAD = 97


# A seam the tests can monkeypatch so no real thread (or provider) is used.
def _spawn(job_id) -> None:
    thread = threading.Thread(
        target=_worker_entrypoint,
        args=(job_id,),
        name=f"videogen-job-{job_id}",
        daemon=True,
    )
    thread.start()


def request_video(page) -> tuple[VideoJob, bool]:
    """Return ``(job, created)`` for the given article page, idempotently.

    If a non-failed job already exists for the page it is returned unchanged and
    no new production is started. Otherwise a fresh job is created and its
    background worker is scheduled to run after the surrounding transaction
    commits.
    """
    specific = page.specific
    script = build_narration_script(specific)
    active = [s.value for s in ACTIVE_STATUSES]

    def _existing():
        return (
            VideoJob.objects.filter(page=page, status__in=active)
            .order_by("-created_at")
            .first()
        )

    try:
        with transaction.atomic():
            existing = (
                VideoJob.objects.select_for_update()
                .filter(page=page, status__in=active)
                .order_by("-created_at")
                .first()
            )
            if existing is not None:
                return existing, False

            job = VideoJob.objects.create(page=page, script=script)
            # Only start the worker once the row is safely committed.
            transaction.on_commit(lambda: _spawn(job.id))
            return job, True
    except IntegrityError:
        # Lost a race with a concurrent request: the unique "one active job per
        # page" constraint fired. Return the job that won the race.
        existing = _existing()
        if existing is not None:
            return existing, False
        raise


def _worker_entrypoint(job_id) -> None:
    """Thread target: run the job, guaranteeing the DB connection is closed."""
    try:
        run_job(job_id)
    finally:
        connection.close()


def run_job(job_id) -> None:
    """Produce the video for ``job_id`` end to end (synchronous).

    Safe to call directly (e.g. from tests or a management command). Any
    provider failure is caught and recorded on the job as a typed error message
    so it is retrievable through the API; the job is never left dangling.
    """
    try:
        job = VideoJob.objects.get(pk=job_id)
    except VideoJob.DoesNotExist:
        logger.warning("VideoJob %s vanished before it could run.", job_id)
        return

    if job.status != JobStatus.PENDING:
        # Already running/finished (e.g. duplicate spawn); nothing to do.
        return

    provider = None
    try:
        provider = VideoGenProvider()
        job.mark_processing()
        job.set_progress(_PROGRESS_START)

        started = provider.start_script_to_video(job.script)
        job.mark_processing(
            provider_workflow_run_id=started.workflow_run_id,
            provider_project_id=started.project_id,
        )

        def _on_run_progress(pct: float) -> None:
            span = _RUN_PROGRESS_CEILING - _RUN_PROGRESS_FLOOR
            job.set_progress(_RUN_PROGRESS_FLOOR + (pct / 100.0) * span)

        provider.wait_for_run(started.workflow_run_id, on_progress=_on_run_progress)
        job.set_progress(_RUN_PROGRESS_CEILING)

        finished = provider.export_video(started.project_id)
        job.provider_export_id = finished.export_id
        job.provider_export_file_id = finished.export_file_id or ""
        job.save(update_fields=["provider_export_id", "provider_export_file_id", "updated_at"])
        job.set_progress(_PROGRESS_EXPORT)

        with tempfile.TemporaryFile() as tmp:
            provider.download_to(finished.download_url, tmp)
            tmp.seek(0)
            job.set_progress(_PROGRESS_DOWNLOAD)
            job.video_file.save(f"{job.pk}.mp4", File(tmp), save=False)
            job.mark_succeeded()

        logger.info("VideoJob %s completed (page %s).", job.pk, job.page_id)
    except VideoGenIntegrationError as exc:
        logger.warning("VideoJob %s failed: %s", job.pk, exc)
        job.mark_failed(exc.message)
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception("VideoJob %s failed unexpectedly.", job.pk)
        job.mark_failed(f"Unexpected error: {exc}")
    finally:
        if provider is not None:
            provider.close()
