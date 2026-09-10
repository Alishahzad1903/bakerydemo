"""Orchestration of a single video production, driven off-request.

Producing a video is a multi-step provider conversation (start workflow -> poll
generation -> start export -> poll export). The site has no task queue or broker
(and must not grow one), so the pipeline runs in a daemon thread started by the
POST handler. The request returns immediately with a ``videoJobId`` while the
thread advances the :class:`ArticleVideo` row to a terminal state.

The pipeline produces **exactly one** billed workflow and **exactly one** export
per job; neither POST is retried, and the job is only ever started once (guarded
by the one-to-one page relation in the view).
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Any

from django.db import connection

from .client import (
    STATUS_SUCCEEDED,
    TERMINAL_FAILURE_STATUSES,
    VideoGenClient,
)
from .exceptions import VideoGenError
from .models import ArticleVideo

logger = logging.getLogger("bakerydemo.videogen")

#: Seconds between provider status polls.
POLL_INTERVAL_SECONDS = 5.0
#: Overall wall-clock ceiling for one production before it is marked failed.
MAX_WAIT_SECONDS = 30 * 60

# Overall progress is split between the two provider phases so the reported
# percentage advances monotonically across the whole production.
_GENERATION_RANGE = (1, 70)
_EXPORT_RANGE = (70, 99)


def start_video_production(article_video_id) -> threading.Thread:
    """Start producing the video for ``article_video_id`` in a daemon thread."""
    thread = threading.Thread(
        target=_worker,
        args=(article_video_id,),
        name=f"videogen-{article_video_id}",
        daemon=True,
    )
    thread.start()
    return thread


def get_fresh_download_url(video: ArticleVideo) -> str | None:
    """Re-fetch the export to obtain a freshly signed download URL.

    The provider re-signs URLs on read, so calling this keeps the MP4
    downloadable for as long as the article (and its project) exist. The newly
    signed URL is cached on the row. Raises :class:`VideoGenError` on provider
    failure so callers can fall back to the last cached URL.
    """
    client = VideoGenClient.from_settings()
    export = client.get_project_export(video.project_id, video.export_id)
    url = export.get("downloadUrl")
    if url:
        video.download_url = url
        video.download_url_expires_at = _epoch_to_datetime(
            export.get("downloadUrlExpiresAt")
        )
        video.save(
            update_fields=["download_url", "download_url_expires_at", "updated_at"]
        )
    return url


# -- Worker ----------------------------------------------------------------


def _worker(article_video_id) -> None:
    try:
        _produce(article_video_id)
    except VideoGenError as exc:
        logger.warning("Video production failed for %s: %s", article_video_id, exc)
        _mark_failed(article_video_id, exc.message)
    except Exception:  # pragma: no cover - defensive backstop
        logger.exception("Unexpected error producing video %s", article_video_id)
        _mark_failed(article_video_id, "Unexpected error during video production.")
    finally:
        # This thread owns its own DB connection; close it so it is not leaked.
        connection.close()


def _produce(article_video_id) -> None:
    video = ArticleVideo.objects.get(pk=article_video_id)
    client = VideoGenClient.from_settings()
    deadline = time.monotonic() + MAX_WAIT_SECONDS

    _update(video, status=ArticleVideo.Status.PROCESSING, progress=_GENERATION_RANGE[0])

    # 1. Start the (single, billed) script-to-video workflow.
    run = client.create_script_to_video(
        script=video.script,
        visual_style={"type": "STOCK"},
    )
    video.workflow_run_id = run.get("workflowRunId", "") or ""
    video.project_id = run.get("projectId", "") or ""
    video.save(update_fields=["workflow_run_id", "project_id", "updated_at"])

    if not video.workflow_run_id:
        raise VideoGenError("VideoGen did not return a workflowRunId.")

    # 2. Poll generation to completion.
    project_id = _poll_workflow_run(client, video, deadline)
    if project_id is None:
        return  # terminal failure already recorded
    if not project_id:
        raise VideoGenError("VideoGen did not return a projectId for the run.")
    video.project_id = project_id
    video.save(update_fields=["project_id", "updated_at"])

    # 3. Start the (single) 720p export.
    export = client.export_project(project_id, quality="STANDARD")
    video.export_id = export.get("exportId", "") or ""
    video.save(update_fields=["export_id", "updated_at"])
    if not video.export_id:
        raise VideoGenError("VideoGen did not return an exportId.")

    # 4. Poll the export and record the finished MP4.
    _poll_export(client, video, deadline)


def _poll_workflow_run(
    client: VideoGenClient, video: ArticleVideo, deadline: float
) -> str | None:
    """Poll the workflow run. Returns the projectId, or None on terminal failure."""
    while True:
        run = client.get_workflow_run(video.workflow_run_id)
        status = run.get("status")
        _update(
            video,
            progress=_scale(run.get("progressPercentage"), *_GENERATION_RANGE),
        )
        if status == STATUS_SUCCEEDED:
            return run.get("projectId") or video.project_id
        if status in TERMINAL_FAILURE_STATUSES:
            _mark_failed(
                video.pk,
                _extract_error(run) or f"Video generation {status}.",
            )
            return None
        if not _wait(deadline):
            _mark_failed(video.pk, "Timed out waiting for video generation.")
            return None


def _poll_export(
    client: VideoGenClient, video: ArticleVideo, deadline: float
) -> None:
    while True:
        export = client.get_project_export(video.project_id, video.export_id)
        status = export.get("status")
        _update(
            video,
            progress=_scale(export.get("progressPercentage"), *_EXPORT_RANGE),
        )
        if status == STATUS_SUCCEEDED:
            video.download_url = export.get("downloadUrl", "") or ""
            video.download_url_expires_at = _epoch_to_datetime(
                export.get("downloadUrlExpiresAt")
            )
            _update(
                video,
                status=ArticleVideo.Status.READY,
                progress=100,
                extra_fields=["download_url", "download_url_expires_at"],
            )
            return
        if status in TERMINAL_FAILURE_STATUSES:
            _mark_failed(
                video.pk,
                _extract_error(export) or f"Video export {status}.",
            )
            return
        if not _wait(deadline):
            _mark_failed(video.pk, "Timed out waiting for the video export.")
            return


# -- Helpers ---------------------------------------------------------------


def _wait(deadline: float) -> bool:
    """Sleep one poll interval. Returns False if the deadline has passed."""
    if time.monotonic() >= deadline:
        return False
    time.sleep(POLL_INTERVAL_SECONDS)
    return time.monotonic() < deadline


def _update(
    video: ArticleVideo,
    *,
    status: str | None = None,
    progress: int | None = None,
    extra_fields: list[str] | None = None,
) -> None:
    fields = list(extra_fields or [])
    if status is not None:
        video.status = status
        fields.append("status")
    if progress is not None:
        # Never let the reported progress go backwards.
        progress = max(video.progress_percentage, int(progress))
        if progress != video.progress_percentage:
            video.progress_percentage = progress
        fields.append("progress_percentage")
    if not fields:
        return
    fields.append("updated_at")
    video.save(update_fields=sorted(set(fields)))


def _mark_failed(article_video_id, message: str) -> None:
    updated = ArticleVideo.objects.filter(pk=article_video_id).update(
        status=ArticleVideo.Status.FAILED,
        error=message or "Video production failed.",
    )
    if not updated:  # pragma: no cover - row deleted mid-flight
        logger.warning("ArticleVideo %s vanished before it could fail", article_video_id)


def _scale(percentage: Any, low: int, high: int) -> int:
    try:
        pct = max(0, min(100, int(percentage)))
    except (TypeError, ValueError):
        pct = 0
    return int(low + (high - low) * pct / 100)


def _extract_error(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("message") or error.get("code") or str(error)
    if isinstance(error, str):
        return error
    return ""


def _epoch_to_datetime(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(value), tz=datetime.UTC)
    except (TypeError, ValueError, OSError):
        return None
