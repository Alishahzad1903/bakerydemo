"""Orchestration for producing one article video, end to end.

Responsibilities:

* build the tiny narration script from the article's own words;
* create (idempotently) exactly one :class:`ArticleVideo` per page;
* drive the async VideoGen pipeline - script-to-video -> poll -> export once at
  720p -> poll - recording progress and any provider failure on the job;
* keep the finished MP4's signed download URL fresh for as long as the article
  exists.

Production runs in a background thread so the ``POST`` can return immediately. No
task queue/broker is introduced (none is available on this host, by design); the
job's state lives in the database, which is the source of truth for the ``GET``
endpoint. The pipeline never retries a workflow or export, so it cannot bill twice.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.db import connection

from .client import TERMINAL_FAILURE, TERMINAL_SUCCESS, VideoGenClient
from .exceptions import (
    VideoGenError,
    VideoGenProductionError,
    VideoGenTimeoutError,
)
from .models import ArticleVideo, VideoStatus
from .narration import build_script_for_page

logger = logging.getLogger("bakerydemo.video")

# Overall progress is split between the generation phase and the export phase.
_GENERATION_WEIGHT = 0.7
_EXPORT_WEIGHT = 0.3


def get_client() -> VideoGenClient:
    """Return a VideoGen client built from settings. Patched in tests."""
    return VideoGenClient.from_settings()


# -- idempotent job creation ----------------------------------------------


def start_video_for_page(page) -> tuple[ArticleVideo, bool]:
    """Get-or-create the single :class:`ArticleVideo` for ``page``.

    Returns ``(job, created)``. When ``created`` is ``True`` a background worker
    is launched to produce the video. When ``False`` an existing job is returned
    untouched - no second VideoGen workflow is started and nothing is billed.
    """
    script = build_script_for_page(page)
    job, created = ArticleVideo.objects.get_or_create(
        page=page,
        defaults={
            "script": script,
            "status": VideoStatus.PROCESSING,
            "progress_percentage": 0,
        },
    )
    if created:
        logger.info(
            "Starting VideoGen production for page %s (job %s)", page.pk, job.id
        )
        spawn_worker(job.id)
    else:
        logger.info(
            "Reusing existing video job %s for page %s (status=%s)",
            job.id,
            page.pk,
            job.status,
        )
    return job, created


# -- background worker -----------------------------------------------------


def _run_worker(job_id) -> None:
    """Thread entry point: load the job, produce the video, always close the conn."""
    try:
        job = ArticleVideo.objects.get(pk=job_id)
    except ArticleVideo.DoesNotExist:  # pragma: no cover - defensive
        logger.warning("Video job %s vanished before production started", job_id)
        return
    try:
        run_production_pipeline(job, get_client())
    except Exception:
        logger.exception("Unexpected failure producing video for job %s", job_id)
        try:
            job.mark_failed("An unexpected error occurred during video production.")
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not record failure for job %s", job_id)
    finally:
        # Background threads get their own DB connection; don't leak it.
        connection.close()


def _spawn_thread(job_id) -> None:
    thread = threading.Thread(
        target=_run_worker,
        args=(job_id,),
        name=f"videogen-{job_id}",
        daemon=True,
    )
    thread.start()


# Indirection so tests can run the pipeline synchronously / stub it out.
spawn_worker: Callable[[Any], None] = _spawn_thread


# -- the production pipeline ------------------------------------------------


def run_production_pipeline(job: ArticleVideo, client: VideoGenClient) -> ArticleVideo:
    """Produce the video for ``job`` using ``client``, recording progress/failure.

    Any :class:`VideoGenError` is caught and recorded on the job as a failure so
    the ``GET`` endpoint can report *what went wrong*; the exception is not
    re-raised. Returns the (refreshed) job.
    """
    interval = float(getattr(settings, "VIDEOGEN_POLL_INTERVAL", 5.0))
    timeout = float(getattr(settings, "VIDEOGEN_POLL_TIMEOUT", 900.0))
    try:
        # 1. Kick off script-to-video (stock footage, voice only, 16:9).
        run = client.create_script_to_video(script=job.script)
        job.workflow_run_id = str(run.get("workflowRunId") or "")
        job.project_id = str(run.get("projectId") or "")
        if not job.workflow_run_id:
            raise VideoGenError("VideoGen did not return a workflowRunId.")
        job.save(update_fields=["workflow_run_id", "project_id", "updated_at"])

        # 2. Poll the workflow run to completion.
        final_run = _poll_until_terminal(
            lambda: client.get_workflow_run(job.workflow_run_id),
            on_progress=lambda pct: job.set_progress(int(pct * _GENERATION_WEIGHT)),
            interval=interval,
            timeout=timeout,
            describe="video generation",
        )
        project_id = str(final_run.get("projectId") or job.project_id)
        if not project_id:
            raise VideoGenError("VideoGen succeeded but returned no projectId.")
        if project_id != job.project_id:
            job.project_id = project_id
            job.save(update_fields=["project_id", "updated_at"])
        job.set_progress(int(100 * _GENERATION_WEIGHT))

        # 3. Export exactly once, at 720p (the client's default tier).
        export = client.export_project(project_id)
        job.export_id = str(export.get("exportId") or "")
        if not job.export_id:
            raise VideoGenError("VideoGen did not return an exportId.")
        job.save(update_fields=["export_id", "updated_at"])

        # 4. Poll the export to completion and capture the download URL.
        final_export = _poll_until_terminal(
            lambda: client.get_export(project_id, job.export_id),
            on_progress=lambda pct: job.set_progress(
                int(100 * _GENERATION_WEIGHT + pct * _EXPORT_WEIGHT)
            ),
            interval=interval,
            timeout=timeout,
            describe="video export",
        )
        download_url = final_export.get("downloadUrl")
        if not download_url:
            raise VideoGenError("Export succeeded but VideoGen returned no downloadUrl.")
        job.mark_ready(str(download_url))
        logger.info("Video job %s is ready", job.id)
    except VideoGenError as exc:
        logger.warning("Video job %s failed: %s", job.id, exc)
        job.mark_failed(str(exc))
    return job


def _poll_until_terminal(
    fetch: Callable[[], dict[str, Any]],
    *,
    on_progress: Callable[[int], None],
    interval: float,
    timeout: float,
    describe: str,
) -> dict[str, Any]:
    """Poll ``fetch`` until VideoGen reports a terminal status.

    Returns the final payload on success; raises :class:`VideoGenProductionError`
    on ``failed``/``cancelled`` and :class:`VideoGenTimeoutError` past ``timeout``.
    """
    deadline = time.monotonic() + timeout
    while True:
        data = fetch()
        status = str(data.get("status") or "").lower()
        progress = data.get("progressPercentage")
        if progress is not None:
            try:
                on_progress(int(progress))
            except (TypeError, ValueError):  # pragma: no cover - defensive
                pass
        if status == TERMINAL_SUCCESS:
            return data
        if status in TERMINAL_FAILURE:
            raise VideoGenProductionError(
                _describe_provider_error(data.get("error"))
                or f"VideoGen reported {describe} as {status}."
            )
        if time.monotonic() >= deadline:
            raise VideoGenTimeoutError(
                f"VideoGen {describe} did not finish within {timeout:.0f}s."
            )
        time.sleep(interval)


def _describe_provider_error(error: Any) -> str:
    """Turn VideoGen's ``error`` field (dict or string) into a readable message."""
    if not error:
        return ""
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or ""
        code = error.get("code")
        if message and code:
            return f"{message} (code={code})"
        return message or (f"code={code}" if code else "")
    return str(error)


# -- keeping the download link alive ---------------------------------------


def refresh_download_url_if_stale(
    job: ArticleVideo, client: VideoGenClient | None = None
) -> ArticleVideo:
    """Re-sign the download URL from VideoGen if the cached one may have expired.

    VideoGen signs download URLs for 7 days and re-signs on read, so fetching the
    export again yields a fresh URL. This is a read - it never triggers a second
    export. Provider errors here are swallowed: we keep serving the cached URL.
    """
    if job.status != VideoStatus.READY:
        return job
    if not job.download_url_is_stale:
        return job
    if not (job.project_id and job.export_id):
        return job
    try:
        client = client or get_client()
        export = client.get_export(job.project_id, job.export_id)
    except VideoGenError as exc:
        logger.warning("Could not refresh download URL for job %s: %s", job.id, exc)
        return job
    url = export.get("downloadUrl")
    if url and url != job.download_url:
        job.mark_ready(str(url))
    elif url:
        # Same URL but re-signed; just update the timestamp.
        job.download_url_signed_at = _now()
        job.save(update_fields=["download_url_signed_at", "updated_at"])
    return job


def _now():
    from django.utils import timezone

    return timezone.now()
