"""Run a video production off the request thread.

There is no task queue or broker in this project (and none is to be
introduced), so production runs in a daemon thread within the Django process.
This satisfies "the call does not have to finish before it returns" without new
infrastructure. A durable queue would be the production upgrade, but is out of
scope here.
"""

from __future__ import annotations

import logging
import threading

from django.db import connection

from . import service
from .exceptions import VideoGenError
from .models import ArticleVideo

logger = logging.getLogger(__name__)


def start_in_background(article_video_pk: int) -> threading.Thread:
    thread = threading.Thread(
        target=_run,
        args=(article_video_pk,),
        name=f"videogen-produce-{article_video_pk}",
        daemon=True,
    )
    thread.start()
    return thread


def _run(article_video_pk: int) -> None:
    try:
        try:
            article_video = ArticleVideo.objects.get(pk=article_video_pk)
        except ArticleVideo.DoesNotExist:
            logger.warning("ArticleVideo %s vanished before production", article_video_pk)
            return
        try:
            service.produce_video(article_video)
        except VideoGenError as exc:
            logger.info("Video production failed for %s: %s", article_video_pk, exc)
            _record_failure(article_video_pk, str(exc))
        except Exception:  # pragma: no cover - unexpected
            logger.exception("Unexpected error producing video %s", article_video_pk)
            _record_failure(
                article_video_pk,
                "An unexpected error occurred while producing the video.",
            )
    finally:
        # Each thread uses its own DB connection; close it to avoid leaks.
        connection.close()


def _record_failure(article_video_pk: int, message: str) -> None:
    try:
        article_video = ArticleVideo.objects.get(pk=article_video_pk)
        article_video.mark_failed(message)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Could not record failure for %s", article_video_pk)
