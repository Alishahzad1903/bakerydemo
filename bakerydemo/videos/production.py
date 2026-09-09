"""
Orchestrate one article-video production run, end to end.

Production runs off the request/response cycle: the POST endpoint records the
job and hands off to :func:`start_production_async`, which drives the VideoGen
flow (script-to-video -> export -> download) on a background thread and stores
the finished MP4 locally.

There is intentionally no task queue or broker here — the environment ships
none and forbids adding one. A daemon thread is the pragmatic fit for the demo.
The orchestration is deliberately kept behind :func:`start_production_async`
so it could be swapped for a Celery/RQ task later without touching the API
layer. One consequence of the thread approach: a server restart mid-production
leaves a job stuck in ``processing`` (it will simply never finish); a real
deployment would reconcile such jobs from a durable queue.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import connection

from .exceptions import VideoProductionError
from .models import ArticleVideo, VideoStatus
from .videogen_client import VideoGenClient

logger = logging.getLogger("bakerydemo.videos")

# Progress budget: the script-to-video workflow is the long phase (0-85%),
# export and download cover the rest.
_WORKFLOW_PROGRESS_CEILING = 85


def build_client_from_settings() -> VideoGenClient:
    """Construct a :class:`VideoGenClient` from Django settings (env-backed)."""
    return VideoGenClient(
        api_key=getattr(settings, "VIDEOGEN_API_KEY", None),
        base_url=getattr(settings, "VIDEOGEN_BASE_URL", None),
        export_quality=getattr(settings, "VIDEOGEN_EXPORT_QUALITY", "FULL_HIGH"),
        aspect_ratio=getattr(settings, "VIDEOGEN_ASPECT_RATIO", None),
        voice_id=getattr(settings, "VIDEOGEN_VOICE_ID", None),
        poll_interval_ms=getattr(settings, "VIDEOGEN_POLL_INTERVAL_MS", 3000),
        timeout_ms=getattr(settings, "VIDEOGEN_TIMEOUT_MS", 3_600_000),
    )


def _update(pk, **fields) -> None:
    ArticleVideo.objects.filter(pk=pk).update(**fields)


def _fail(pk, message: str) -> None:
    logger.warning("Article video %s failed: %s", pk, message)
    _update(pk, status=VideoStatus.FAILED, error=message)


def produce_video(article_video_pk) -> None:
    """Run the full VideoGen flow for one :class:`ArticleVideo` row."""
    try:
        av = ArticleVideo.objects.get(pk=article_video_pk)
    except ArticleVideo.DoesNotExist:
        return

    try:
        client = build_client_from_settings()
        _update(article_video_pk, status=VideoStatus.PROCESSING, progress_percentage=1)

        started = client.start_script_to_video(av.script)
        _update(
            article_video_pk,
            workflow_run_id=started["workflow_run_id"],
            project_id=started["project_id"],
        )

        def on_progress(pct: float) -> None:
            scaled = int(pct * _WORKFLOW_PROGRESS_CEILING / 100)
            _update(article_video_pk, progress_percentage=min(scaled, _WORKFLOW_PROGRESS_CEILING))

        client.wait_for_workflow(started["workflow_run_id"], on_progress=on_progress)
        _update(article_video_pk, progress_percentage=_WORKFLOW_PROGRESS_CEILING)

        export_id = client.start_export(started["project_id"])
        _update(article_video_pk, export_id=export_id, progress_percentage=90)

        export = client.wait_for_export(started["project_id"], export_id)
        _update(article_video_pk, progress_percentage=95)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / f"{av.job_id}.mp4"
            client.download_export(export, tmp_path)

            fresh = ArticleVideo.objects.get(pk=article_video_pk)
            with tmp_path.open("rb") as fh:
                fresh.video_file.save(f"{av.job_id}.mp4", File(fh), save=False)
            fresh.status = VideoStatus.READY
            fresh.progress_percentage = 100
            fresh.error = ""
            fresh.save(
                update_fields=[
                    "video_file",
                    "status",
                    "progress_percentage",
                    "error",
                    "updated_at",
                ]
            )
        logger.info("Article video %s ready (page %s)", av.job_id, av.page_id)

    except VideoProductionError as exc:
        _fail(article_video_pk, str(exc))
    except Exception as exc:  # pragma: no cover - defensive last resort
        logger.exception("Unexpected error producing article video %s", article_video_pk)
        _fail(article_video_pk, f"Unexpected error: {exc}")
    finally:
        # This runs on a worker thread; release its DB connection.
        connection.close()


def start_production_async(article_video: ArticleVideo) -> None:
    """Kick off production for ``article_video`` on a background daemon thread."""
    thread = threading.Thread(
        target=produce_video,
        args=(article_video.pk,),
        name=f"videogen-{article_video.job_id}",
        daemon=True,
    )
    thread.start()
