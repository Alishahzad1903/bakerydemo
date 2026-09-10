"""Background execution of the video production pipeline.

The task explicitly rules out a broker/queue, so production runs in a supervised
daemon thread. The POST endpoint returns immediately; this thread drives the job
to completion and records the outcome (including typed provider failures) back
onto the :class:`VideoJob`. The status endpoint reads that persisted state, so a
caller never has to hold a connection open.
"""

from __future__ import annotations

import logging
import threading

from django.db import close_old_connections

from .exceptions import VideoIntegrationError
from .models import VideoJob, VideoJobStatus
from .service import produce

logger = logging.getLogger("bakerydemo.videos")


def _mark_failed(job: VideoJob, message: str) -> None:
    try:
        job.refresh_from_db()
    except VideoJob.DoesNotExist:
        return
    job.status = VideoJobStatus.FAILED
    job.error = message
    job.save(update_fields=["status", "error", "updated_at"])


def _run(job_id) -> None:
    close_old_connections()
    try:
        try:
            job = VideoJob.objects.get(pk=job_id)
        except VideoJob.DoesNotExist:
            return
        try:
            produce(job)
            logger.info("Video job %s ready", job_id)
        except VideoIntegrationError as exc:
            logger.warning("Video job %s failed: %s", job_id, exc)
            _mark_failed(job, str(exc))
        except Exception as exc:
            logger.exception("Unexpected error producing video job %s", job_id)
            _mark_failed(job, f"Unexpected error: {exc}")
    finally:
        close_old_connections()


def start_production(job: VideoJob) -> None:
    """Kick off production for ``job`` in a background daemon thread."""
    thread = threading.Thread(
        target=_run,
        args=(job.pk,),
        name=f"videojob-{job.pk}",
        daemon=True,
    )
    thread.start()
