"""Orchestration: turn an article into a stored, downloadable MP4.

This ties the request-facing model (:class:`ArticleVideo`) to the VideoGen
integration (:mod:`bakerydemo.videos.videogen_service`). Producing a video is
asynchronous — the POST handler returns immediately and the work runs in a
background daemon thread scheduled on transaction commit. No task queue or
broker is used (none is available, and the task forbids introducing one); a
thread is the pragmatic fit for this single-process demo.

The finished MP4 is downloaded from VideoGen's signed URL into the site's own
media storage, so it stays retrievable through the site for as long as the
article exists, independent of the provider's URL expiry.
"""

from __future__ import annotations

import logging
import threading

import httpx
from django.core.files.base import ContentFile
from django.db import connection, transaction

from . import videogen_service
from .exceptions import VideoGenError, VideoGenUnavailable
from .models import ArticleVideo, VideoStatus
from .narration import build_script_for_page

logger = logging.getLogger("bakerydemo.videos")

# Timeout for downloading the finished MP4 from the provider's signed URL.
_DOWNLOAD_TIMEOUT = 120.0


class _ModelHooks:
    """Persist production progress onto an :class:`ArticleVideo` row."""

    def __init__(self, video: ArticleVideo):
        self._video = video

    def workflow_started(self, workflow_run_id: str, project_id: str) -> None:
        self._video.provider_workflow_run_id = workflow_run_id
        self._video.provider_project_id = project_id
        self._video.save(
            update_fields=[
                "provider_workflow_run_id",
                "provider_project_id",
                "updated_at",
            ]
        )

    def export_started(self, export_id: str) -> None:
        self._video.provider_export_id = export_id
        self._video.save(update_fields=["provider_export_id", "updated_at"])

    def progress(self, percentage: int) -> None:
        self._video.update_progress(percentage)


def start_video_for_page(page) -> tuple[ArticleVideo, bool]:
    """Idempotently start (or return) the video job for ``page``.

    Returns ``(article_video, started)`` where ``started`` is ``True`` only
    when a new production was kicked off. A page that already has a video in
    progress or ready returns that existing job unchanged — producing a video
    twice in a row for the same article never starts a second, separately
    billed production. A previously *failed* job is restarted.
    """
    video, created = ArticleVideo.objects.get_or_create(page=page)

    if not created and video.status != VideoStatus.FAILED:
        # In progress or already succeeded: hand back the existing job.
        return video, False

    if not created:
        # Terminal failure: start a fresh attempt for this article.
        video.reset_for_new_attempt()

    script = build_script_for_page(page)
    video.script = script
    video.status = VideoStatus.PENDING
    video.progress = 0
    video.save(update_fields=["script", "status", "progress", "updated_at"])

    enqueue_production(video.pk, script)
    return video, True


def enqueue_production(video_pk: int, script: str) -> None:
    """Schedule the production to run after the current transaction commits.

    Isolated in its own function so tests can patch it to run synchronously
    (or not at all) instead of spawning a real thread against the real API.
    """

    def _spawn():
        thread = threading.Thread(
            target=run_production,
            args=(video_pk, script),
            name=f"videogen-{video_pk}",
            daemon=True,
        )
        thread.start()

    transaction.on_commit(_spawn)


def run_production(video_pk: int, script: str) -> None:
    """Produce the video for ``video_pk`` and store the resulting MP4.

    Runs in a background thread. Every provider failure is a typed
    :class:`VideoGenError`, which is recorded on the row so the GET endpoint can
    report what went wrong.
    """
    try:
        video = ArticleVideo.objects.get(pk=video_pk)
    except ArticleVideo.DoesNotExist:
        logger.warning("ArticleVideo %s vanished before production started.", video_pk)
        return

    try:
        video.mark_processing(progress=1)
        download_url = videogen_service.produce_video(script, hooks=_ModelHooks(video))
        _store_mp4(video, download_url)
        video.mark_succeeded()
        logger.info("Article video %s ready.", video_pk)
    except VideoGenError as exc:
        logger.warning("Article video %s failed: %s", video_pk, exc)
        video.mark_failed(str(exc))
    except Exception:
        logger.exception("Article video %s failed unexpectedly.", video_pk)
        video.mark_failed("An unexpected error occurred while producing the video.")
    finally:
        # A manually spawned thread owns its own DB connection; close it so it
        # is not leaked for the life of the process.
        connection.close()


def _store_mp4(video: ArticleVideo, download_url: str) -> None:
    """Download the finished MP4 from ``download_url`` into media storage."""
    try:
        with httpx.stream("GET", download_url, timeout=_DOWNLOAD_TIMEOUT) as response:
            response.raise_for_status()
            content = response.read()
    except httpx.HTTPError as exc:
        raise VideoGenUnavailable("Could not download the produced MP4.") from exc

    video.mp4.save(f"{video.job_id}.mp4", ContentFile(content), save=True)
