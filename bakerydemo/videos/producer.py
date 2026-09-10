"""Drive a single article-video job to completion, off the request thread.

There is no task queue in this project (and none is to be introduced), so the
work runs in a daemon thread. The producer:

    1. starts a script-to-video workflow run,
    2. polls it to completion (the project is built: stock b-roll + voiceover),
    3. exports the project once, at 720p / 16:9,
    4. polls the export to completion,
    5. records the finished MP4's download URL and marks the job ready.

Each step's failure is a typed :class:`VideoGenError`; the thread records it on
the job as a terminal ``failed`` state rather than letting it vanish. The whole
loop is bounded by ``VIDEOGEN_JOB_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from django.conf import settings
from django.db import close_old_connections

from .exceptions import VideoGenError, VideoGenTimeoutError
from .models import ArticleVideo
from .service import JobSnapshot, VideoGenService

logger = logging.getLogger(__name__)

# Share of overall progress attributed to each phase.
_BUILD_SHARE = 0.5


def start_production(job: ArticleVideo) -> threading.Thread:
    """Kick off production for ``job`` in a daemon thread and return it."""
    thread = threading.Thread(
        target=_run,
        args=(str(job.job_id),),
        name=f"videogen-{job.job_id}",
        daemon=True,
    )
    thread.start()
    return thread


def _run(job_id: str) -> None:
    # A fresh thread must not inherit a stale/closed DB connection.
    close_old_connections()
    try:
        job = ArticleVideo.objects.get(job_id=job_id)
    except ArticleVideo.DoesNotExist:
        logger.warning("videogen: job %s vanished before production started", job_id)
        close_old_connections()
        return

    try:
        produce(job)
    except VideoGenError as exc:
        logger.warning("videogen: job %s failed: %s", job_id, exc)
        _mark_failed(job, str(exc))
    except Exception:  # last-resort guard so a daemon thread never dies silently
        logger.exception("videogen: job %s crashed unexpectedly", job_id)
        _mark_failed(job, "An unexpected internal error occurred while producing the video.")
    finally:
        close_old_connections()


def produce(job: ArticleVideo, service: VideoGenService | None = None) -> None:
    """Run the full production flow for ``job`` (synchronously).

    Separated from :func:`_run` so it can be exercised directly in tests with a
    stubbed service.
    """
    service = service or VideoGenService()

    # 1. Start the workflow run.
    start = service.start_script_to_video(job.script)
    job.workflow_run_id = start.workflow_run_id
    job.project_id = start.project_id
    job.save(update_fields=["workflow_run_id", "project_id", "updated_at"])

    # 2. Poll the workflow run until the project is built (0 -> 50%).
    run = _poll(
        lambda: service.get_workflow_run(start.workflow_run_id),
        job,
        base=0.0,
        span=_BUILD_SHARE * 100.0,
    )
    if not run.is_succeeded:
        _mark_failed(job, _terminal_reason("Video build", run))
        return

    # 3. Export the built project once, at 720p / 16:9.
    export_id = service.export_project(start.project_id)
    job.export_id = export_id
    job.save(update_fields=["export_id", "updated_at"])

    # 4. Poll the export until the MP4 is rendered (50 -> 100%).
    export = _poll(
        lambda: service.get_project_export(start.project_id, export_id),
        job,
        base=_BUILD_SHARE * 100.0,
        span=(1.0 - _BUILD_SHARE) * 100.0,
    )
    if not export.is_succeeded:
        _mark_failed(job, _terminal_reason("Video export", export))
        return

    if not export.download_url:
        _mark_failed(job, "Video export succeeded but no download URL was returned.")
        return

    # 5. Ready.
    job.status = ArticleVideo.Status.READY
    job.progress_percentage = 100.0
    job.download_url = export.download_url
    job.error = ""
    job.save(
        update_fields=["status", "progress_percentage", "download_url", "error", "updated_at"]
    )
    logger.info("videogen: job %s is ready", job.job_id)


def _poll(
    fetch: Callable[[], JobSnapshot],
    job: ArticleVideo,
    *,
    base: float,
    span: float,
) -> JobSnapshot:
    """Poll ``fetch`` until the snapshot is terminal or the time budget is spent.

    Overall progress is reported as ``base + snapshot_progress% * span``.
    """
    interval = getattr(settings, "VIDEOGEN_POLL_INTERVAL_SECONDS", 6.0)
    timeout = getattr(settings, "VIDEOGEN_JOB_TIMEOUT_SECONDS", 1800.0)
    deadline = time.monotonic() + timeout

    while True:
        snapshot = fetch()
        overall = base + (snapshot.progress_percentage / 100.0) * span
        _update_progress(job, overall)

        if snapshot.is_terminal:
            return snapshot

        if time.monotonic() >= deadline:
            raise VideoGenTimeoutError(
                f"VideoGen job did not finish within {timeout:.0f}s "
                f"(last status: {snapshot.status})."
            )
        time.sleep(interval)


def _update_progress(job: ArticleVideo, overall: float) -> None:
    # Progress only moves forward and never reaches 100 before the job is ready.
    clamped = max(0.0, min(99.0, overall))
    if clamped > job.progress_percentage:
        job.progress_percentage = clamped
        job.save(update_fields=["progress_percentage", "updated_at"])


def _terminal_reason(phase: str, snapshot: JobSnapshot) -> str:
    detail = snapshot.error_message or f"status {snapshot.status}"
    return f"{phase} did not succeed: {detail}"


def _mark_failed(job: ArticleVideo, message: str) -> None:
    job.status = ArticleVideo.Status.FAILED
    job.error = message
    job.save(update_fields=["status", "error", "updated_at"])
