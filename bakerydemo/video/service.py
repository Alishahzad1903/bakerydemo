"""
Orchestration of a single article -> video production run.

Pipeline (all VideoGen interactions go through the official SDK, the skill's
sole reference for the API):

1. ``script_to_video`` — turn the narration (the article's own words) into a
   narrated video. The workflow uses stock footage (never AI-generated imagery)
   and no remix actions are applied (no captions, music, image-to-video,
   upscales, avatars). Aspect ratio is left at the API default of 16:9.

   GAP NOTE — stock-footage visual style: VideoGen's script-to-video workflow
   *requires* a ``visualStyle``, but the VideoGen ``api`` skill that is this
   integration's sole reference documents only the AI-image style (``AI_IMAGE``),
   which this site must not use. The value that selects stock footage is not in
   the skill, not in the SDK, not enumerable from the provider's validation
   error, and VideoGen web/docs lookups are blocked in this workspace. Rather
   than invent an enum value or fall back to the forbidden AI style, the value is
   supplied as operator configuration (``VIDEOGEN_VISUAL_STYLE_TYPE``). Left
   unset, production fails fast with a typed configuration error.
2. ``export_project`` — export the finished project to MP4 exactly once. No
   second export and no 4K/quality escalation is ever requested.
3. ``download_file`` — download the exported MP4 by its file id and store it on
   the site so it stays retrievable for as long as the article exists.

Every provider failure is surfaced as a typed exception (see ``exceptions.py``).
The producer runs on a background thread so the POST that starts it can return
immediately.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import connection
from videogen import poll_project_export, poll_workflow_run
from videogen.errors import PollCancelledError
from videogen.errors import VideoGenError as ProviderVideoGenError

from .client import build_client, get_visual_style
from .exceptions import (
    VideoGenCancelledError,
    VideoGenServiceError,
    VideoGenTimeoutError,
)
from .models import ArticleVideo

logger = logging.getLogger("bakerydemo.video")

# Progress checkpoints (0-100) reported to callers as the run advances. The
# script-to-video phase is the long one, so it owns most of the range.
_PROGRESS_SCRIPT_CEILING = 80
_PROGRESS_EXPORT_STARTED = 85
_PROGRESS_EXPORT_DONE = 95


@contextmanager
def _translate_provider_errors(stage: str):
    """Re-raise raw SDK/transport failures as typed integration exceptions."""
    try:
        yield
    except ProviderVideoGenError as exc:
        raise VideoGenServiceError(
            str(exc),
            stage=stage,
            status=getattr(exc, "status", None),
            provider_body=getattr(exc, "body", None),
            request_id=getattr(exc, "request_id", None),
        ) from exc
    except PollCancelledError as exc:
        raise VideoGenCancelledError(str(exc) or "Cancelled.", stage=stage) from exc
    except TimeoutError as exc:
        raise VideoGenTimeoutError(str(exc) or "Timed out.", stage=stage) from exc


def _timeout_ms() -> int:
    seconds = getattr(settings, "VIDEOGEN_JOB_TIMEOUT_SECONDS", 1800)
    return int(seconds) * 1000


def produce_and_store(article_video: ArticleVideo) -> None:
    """
    Run the full production pipeline for ``article_video``, updating the row as
    it advances and storing the finished MP4 on success.

    Raises a :class:`VideoGenIntegrationError` subclass on any provider failure;
    the caller is responsible for persisting that as the row's failure reason.
    """
    client = build_client()
    # Resolve the stock-footage visual style before touching the provider so a
    # misconfiguration fails fast (and clearly) rather than as a provider 400.
    visual_style = get_visual_style()
    timeout_ms = _timeout_ms()

    article_video.status = ArticleVideo.Status.PROCESSING
    article_video.progress_percentage = 0
    article_video.error = ""
    article_video.save(
        update_fields=["status", "progress_percentage", "error", "updated_at"]
    )

    # 1. Script -> video. Start first so the run id is persisted before we block
    #    polling, which keeps the run diagnosable even if this process dies.
    with _translate_provider_errors("script_to_video"):
        started = client.workflows.script_to_video(
            script=article_video.script,
            visual_style=visual_style,
        )
    article_video.workflow_run_id = started.get("workflow_run_id", "") or ""
    article_video.project_id = started.get("project_id", "") or ""
    article_video.save(
        update_fields=["workflow_run_id", "project_id", "updated_at"]
    )

    def on_progress(pct: float) -> None:
        mapped = int(min(pct, 100.0) / 100.0 * _PROGRESS_SCRIPT_CEILING)
        _update_progress(article_video, mapped)

    with _translate_provider_errors("script_to_video"):
        run = poll_workflow_run(
            client,
            article_video.workflow_run_id,
            on_progress=on_progress,
            timeout_ms=timeout_ms,
        )

    project_id = run.get("project_id") or article_video.project_id
    if not project_id:
        raise VideoGenServiceError(
            "VideoGen did not return a project id for the finished workflow run.",
            stage="script_to_video",
            provider_body=run,
        )
    article_video.project_id = project_id

    # 2. Export the finished project to MP4 exactly once.
    with _translate_provider_errors("export"):
        export_started = client.projects.export_project(project_id=project_id)
    article_video.export_id = export_started.get("export_id", "") or ""
    _update_progress(
        article_video,
        _PROGRESS_EXPORT_STARTED,
        extra_fields=["project_id", "export_id"],
    )

    with _translate_provider_errors("export"):
        export = poll_project_export(
            client,
            project_id,
            article_video.export_id,
            timeout_ms=timeout_ms,
        )

    export_file_id = export.get("export_file_id")
    if not export_file_id:
        raise VideoGenServiceError(
            "VideoGen export finished without an export file id.",
            stage="export",
            provider_body=export,
        )
    article_video.export_file_id = export_file_id
    _update_progress(
        article_video, _PROGRESS_EXPORT_DONE, extra_fields=["export_file_id"]
    )

    # 3. Download the exported MP4 and store it on the site.
    with _translate_provider_errors("download"):
        content = client.download_file(export_file_id)

    article_video.video_file.save(
        f"{article_video.job_id}.mp4", ContentFile(content), save=False
    )
    article_video.status = ArticleVideo.Status.READY
    article_video.progress_percentage = 100
    article_video.error = ""
    article_video.save(
        update_fields=[
            "video_file",
            "status",
            "progress_percentage",
            "error",
            "updated_at",
        ]
    )


def _update_progress(article_video, pct: int, extra_fields=None) -> None:
    # Never let progress go backwards.
    pct = max(pct, article_video.progress_percentage)
    article_video.progress_percentage = pct
    fields = ["progress_percentage", "updated_at"]
    if extra_fields:
        fields = list(dict.fromkeys(extra_fields + fields))
    try:
        article_video.save(update_fields=fields)
    except Exception:  # pragma: no cover - progress updates are best-effort
        logger.debug("Failed to persist progress update", exc_info=True)


def _run_job(article_video_pk: int) -> None:
    """Background entry point: produce the video and persist the outcome."""
    try:
        article_video = ArticleVideo.objects.get(pk=article_video_pk)
    except ArticleVideo.DoesNotExist:  # pragma: no cover - defensive
        logger.warning("ArticleVideo %s vanished before production", article_video_pk)
        return

    try:
        produce_and_store(article_video)
        logger.info(
            "Article video ready: page=%s job=%s", article_video.page_id,
            article_video.job_id,
        )
    except Exception as exc:
        logger.exception("Article video production failed: %s", exc)
        try:
            article_video.mark_failed(str(exc))
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to record video failure")
    finally:
        # This thread owns its own DB connection; close it so SQLite/Postgres
        # do not accumulate idle connections.
        connection.close()


def start_async_production(article_video: ArticleVideo) -> None:
    """
    Start producing the video on a daemon thread.

    There is deliberately no task queue or broker: the site has no such infra
    and the task forbids introducing one. A daemon thread keeps the POST
    non-blocking while the (single) production runs.
    """
    thread = threading.Thread(
        target=_run_job,
        args=(article_video.pk,),
        name=f"videogen-{article_video.job_id}",
        daemon=True,
    )
    thread.start()
