"""Orchestration: turn one published article into a downloadable MP4.

Producing a video is a two-phase asynchronous job on VideoGen:

1. **build** — ``script_to_video`` starts a workflow run; poll it to completion;
2. **export** — ``export_project`` renders the MP4 at 720p; poll it to completion.

Both phases are billed once. Because the project forbids a broker/queue, the work
runs in an in-process daemon thread started after the request's transaction
commits. The HTTP ``POST`` returns immediately with a ``videoJobId``; the thread
updates the :class:`~bakerydemo.videos.models.ArticleVideo` row as it progresses.

Cost safety is structural: ``request_video`` is idempotent per page (a second
request never starts a second job), and the worker only issues each billed call
when its provider id is still empty — so a job can never bill the same step twice.
No billed call is ever retried.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable

from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone

from .exceptions import (
    VideoGenAPIError,
    VideoGenError,
    VideoGenJobFailedError,
    VideoGenTimeoutError,
    VideoGenTransportError,
)
from .models import ArticleVideo
from .narration import build_narration_script
from .provider import VideoGenService

logger = logging.getLogger("bakerydemo.videos")

# Polling cadence and budgets.
POLL_INTERVAL_SECONDS = 5.0
PHASE_BUDGET_SECONDS = 30 * 60  # per phase; then VideoGenTimeoutError (no re-bill)
MAX_CONSECUTIVE_POLL_ERRORS = 5
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Progress weighting: the build phase drives 0-90%, the export phase 90-100%.
_BUILD_WEIGHT = (0.0, 90.0)
_EXPORT_WEIGHT = (90.0, 100.0)

_TERMINAL_OK = "succeeded"
_TERMINAL_BAD = frozenset({"failed", "cancelled"})

Launcher = Callable[[int], object]


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def request_video(page, *, launcher: Launcher | None = None) -> ArticleVideo:
    """Idempotently start (or return) the video job for ``page``.

    If a job already exists and has not failed, it is returned unchanged and no
    provider call is made — asking twice never produces or bills a second video.
    A previously *failed* job is restarted in place with a fresh ``job_id``.
    """
    launch = launcher or _launch_worker
    script = build_narration_script(
        title=page.title, introduction=getattr(page, "introduction", "") or ""
    )

    with transaction.atomic():
        article_video, created = ArticleVideo.objects.get_or_create(
            page=page,
            defaults={"script": script, "status": ArticleVideo.Status.PROCESSING},
        )
        should_launch = created
        if not created and article_video.status == ArticleVideo.Status.FAILED:
            # Restart a failed job: reuse the row, mint a new public job id.
            article_video.job_id = uuid.uuid4()
            article_video.script = script
            article_video.status = ArticleVideo.Status.PROCESSING
            article_video.progress_percentage = 0.0
            article_video.workflow_run_id = ""
            article_video.project_id = ""
            article_video.export_id = ""
            article_video.download_url = ""
            article_video.error = ""
            article_video.save()
            should_launch = True

        if should_launch:
            pk = article_video.pk
            transaction.on_commit(lambda: launch(pk))

    return article_video


def get_job_state(page, job_id: uuid.UUID) -> ArticleVideo:
    """Return the :class:`ArticleVideo` for ``page`` and ``job_id`` or raise 404."""
    try:
        return ArticleVideo.objects.get(page=page, job_id=job_id)
    except ArticleVideo.DoesNotExist as exc:
        raise Http404("No such video job for this article.") from exc


def fresh_download_url(
    article_video: ArticleVideo, *, service: VideoGenService | None = None
) -> str | None:
    """Return a currently-valid signed MP4 URL for a READY job.

    VideoGen re-signs the download URL on read, so we fetch it live to keep the
    link valid for as long as the article exists. If the live fetch fails we fall
    back to the last-known URL rather than failing the status read.
    """
    if article_video.status != ArticleVideo.Status.READY:
        return None
    try:
        service = service or VideoGenService()
        export = service.get_project_export(
            article_video.project_id, article_video.export_id
        )
        url = export.download_url
        if url:
            if url != article_video.download_url:
                ArticleVideo.objects.filter(pk=article_video.pk).update(download_url=url)
            return url
    except VideoGenError as exc:
        logger.warning(
            "Could not refresh download URL for job %s: %s", article_video.job_id, exc
        )
    return article_video.download_url or None


# --------------------------------------------------------------------------- #
# Background worker
# --------------------------------------------------------------------------- #


def _launch_worker(pk: int) -> threading.Thread:
    thread = threading.Thread(
        target=_worker_main, args=(pk,), name=f"videogen-job-{pk}", daemon=True
    )
    thread.start()
    return thread


def _worker_main(pk: int) -> None:
    """Thread entry point: run the job, then release this thread's DB connection."""
    try:
        run_job(pk)
    finally:
        # Important under SQLite: don't leak this thread's connection.
        connection.close()


def run_job(pk: int, *, service: VideoGenService | None = None) -> None:
    """Drive one video job to a terminal state, translating failures to status.

    Safe to call directly (e.g. in tests); the DB-connection cleanup that a
    background thread needs lives in :func:`_worker_main`, not here.
    """
    try:
        service = service or VideoGenService()
        _drive_job(pk, service)
    except VideoGenError as exc:
        logger.warning("Video job %s failed: %s", pk, exc)
        _mark_failed(pk, str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in video job %s", pk)
        _mark_failed(pk, f"Unexpected error: {exc}")


def _drive_job(pk: int, service: VideoGenService) -> None:
    article_video = ArticleVideo.objects.get(pk=pk)

    # Phase 1 — build (billed). Only start if not already started.
    if not article_video.workflow_run_id:
        started = service.start_script_to_video(script=article_video.script)
        _update(
            pk,
            workflow_run_id=started.workflow_run_id,
            project_id=started.project_id,
            status=ArticleVideo.Status.PROCESSING,
        )

    _poll(
        pk,
        phase="build",
        weight=_BUILD_WEIGHT,
        fetch=lambda: service.get_workflow_run(_reload(pk).workflow_run_id),
    )

    # Phase 2 — export (billed). Only start if not already started.
    article_video = _reload(pk)
    if not article_video.export_id:
        exported = service.export_project(article_video.project_id)
        _update(pk, export_id=exported.export_id)

    export = _poll(
        pk,
        phase="export",
        weight=_EXPORT_WEIGHT,
        fetch=lambda: service.get_project_export(
            _reload(pk).project_id, _reload(pk).export_id
        ),
    )

    download_url = getattr(export, "download_url", None) or ""
    _update(
        pk,
        status=ArticleVideo.Status.READY,
        progress_percentage=100.0,
        download_url=download_url,
        error="",
    )


def _poll(pk: int, *, phase: str, weight: tuple[float, float], fetch: Callable[[], object]):
    """Poll ``fetch`` until the provider job reaches a terminal state.

    Transient transport/5xx/429 errors are tolerated up to
    :data:`MAX_CONSECUTIVE_POLL_ERRORS` consecutive failures; a 4xx or repeated
    failure is fatal. No billed call is issued here, so polling never re-bills.
    """
    deadline = time.monotonic() + PHASE_BUDGET_SECONDS
    weight_lo, weight_hi = weight
    consecutive_errors = 0

    while True:
        try:
            obj = fetch()
            consecutive_errors = 0
        except VideoGenTransportError as exc:
            consecutive_errors += 1
            if consecutive_errors > MAX_CONSECUTIVE_POLL_ERRORS:
                raise
            logger.warning("Transient error polling %s job %s: %s", phase, pk, exc)
            _sleep_until(deadline)
            continue
        except VideoGenAPIError as exc:
            if exc.status_code in _RETRYABLE_STATUS:
                consecutive_errors += 1
                if consecutive_errors > MAX_CONSECUTIVE_POLL_ERRORS:
                    raise
                logger.warning("Retryable %s polling %s job %s", exc.status_code, phase, pk)
                _sleep_until(deadline)
                continue
            raise

        status = _status_value(getattr(obj, "status", ""))
        progress = float(getattr(obj, "progress_percentage", 0.0) or 0.0)
        overall = weight_lo + (weight_hi - weight_lo) * (max(0.0, min(progress, 100.0)) / 100.0)
        _update(pk, progress_percentage=round(overall, 2))

        if status == _TERMINAL_OK:
            return obj
        if status in _TERMINAL_BAD:
            message = _error_message(obj) or f"VideoGen {phase} {status}."
            raise VideoGenJobFailedError(message, status=status)

        if time.monotonic() >= deadline:
            raise VideoGenTimeoutError(
                f"VideoGen {phase} did not finish within the allotted time."
            )
        time.sleep(POLL_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _reload(pk: int) -> ArticleVideo:
    return ArticleVideo.objects.get(pk=pk)


def _update(pk: int, **fields) -> None:
    fields.setdefault("updated_at", timezone.now())
    ArticleVideo.objects.filter(pk=pk).update(**fields)


def _mark_failed(pk: int, message: str) -> None:
    _update(pk, status=ArticleVideo.Status.FAILED, error=message[:2000])


def _status_value(status) -> str:
    """Return the wire status string for a ``JobStatus`` enum or plain str."""
    return str(getattr(status, "value", status)).lower()


def _error_message(obj) -> str:
    error = getattr(obj, "error", None)
    if error is None:
        return ""
    message = getattr(error, "message", "") or ""
    code = getattr(error, "code", None)
    code = code if isinstance(code, str) else None
    return f"{message} ({code})" if code else str(message)


def _sleep_until(deadline: float) -> None:
    """Sleep one poll interval unless the deadline is closer."""
    remaining = deadline - time.monotonic()
    time.sleep(max(0.0, min(POLL_INTERVAL_SECONDS, remaining)))
