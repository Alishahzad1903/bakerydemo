"""Orchestration for producing an article video.

The public entry point is :func:`start_video_for_page`, which is idempotent per
page and returns immediately. The actual VideoGen work (render -> single export
-> download the MP4) runs on a background daemon thread so the HTTP request can
return without waiting. No task queue/broker is used, per the environment
constraints.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time

from django.conf import settings
from django.core.files import File
from django.db import connections, transaction

from bakerydemo.blog.models import BlogPage

from .exceptions import (
    VideoGenConfigurationError,
    VideoGenError,
    VideoGenJobFailedError,
    VideoGenTimeoutError,
)
from .models import ArticleVideo
from .narration import build_narration_script
from .videogen import TERMINAL_STATUSES, VideoGenClient

logger = logging.getLogger("bakerydemo.videos")

# Progress is a single 0-100 number spanning the whole pipeline. These are the
# boundaries between phases so the number climbs smoothly end to end.
_RENDER_BASE, _RENDER_SPAN = 5, 50  # 5 -> 55 while the workflow renders
_EXPORT_BASE, _EXPORT_SPAN = 55, 35  # 55 -> 90 while the MP4 exports
_DOWNLOAD_PROGRESS = 93  # while the MP4 is fetched into local storage


def get_videogen_client() -> VideoGenClient:
    """Build a client from settings, or raise if the key is not configured."""
    api_key = getattr(settings, "VIDEOGEN_API_KEY", "")
    if not api_key:
        raise VideoGenConfigurationError(
            "VIDEOGEN_API_KEY is not set; cannot talk to VideoGen."
        )
    return VideoGenClient(
        api_key=api_key,
        base_url=getattr(settings, "VIDEOGEN_BASE_URL", "https://api.videogen.io"),
        timeout=getattr(settings, "VIDEOGEN_HTTP_TIMEOUT_SECONDS", 60),
    )


def get_publishable_blog_article(page_id: int) -> BlogPage | None:
    """Return the live BlogPage with ``page_id`` (specific), or ``None``.

    Only published (``live``) blog articles can be turned into a video.
    """
    return BlogPage.objects.live().filter(pk=page_id).specific().first()


def start_video_for_page(page: BlogPage) -> tuple[ArticleVideo, bool]:
    """Idempotently start (or return) the video job for ``page``.

    Returns ``(article_video, created)``. When ``created`` is ``True`` a new
    background render was launched; when ``False`` an existing job (in any
    state) is returned untouched — so a second request never produces or bills
    a second video.
    """
    # Fail fast on misconfiguration before creating any state.
    if not getattr(settings, "VIDEOGEN_API_KEY", ""):
        raise VideoGenConfigurationError(
            "VIDEOGEN_API_KEY is not set; cannot talk to VideoGen."
        )

    script = build_narration_script(
        page, max_words=getattr(settings, "VIDEOGEN_NARRATION_MAX_WORDS", 30)
    )

    with transaction.atomic():
        video, created = ArticleVideo.objects.select_for_update().get_or_create(
            page=page,
            defaults={"script": script, "status": ArticleVideo.Status.PENDING},
        )

    if created:
        launch_pipeline(video.pk)
    return video, created


# The pipeline launcher is a module attribute so tests can replace it.
def launch_pipeline(video_pk: int) -> None:
    """Run the production pipeline for ``video_pk`` on a daemon thread."""
    thread = threading.Thread(
        target=run_pipeline,
        args=(video_pk,),
        name=f"videogen-pipeline-{video_pk}",
        daemon=True,
    )
    thread.start()


def run_pipeline(video_pk: int) -> None:
    """Drive one job to a terminal state, translating failures to the model.

    Any :class:`VideoGenError` (and any unexpected error) is recorded on the
    row as a failure; the row is never left silently stuck.
    """
    try:
        video = ArticleVideo.objects.get(pk=video_pk)
    except ArticleVideo.DoesNotExist:  # pragma: no cover - defensive
        logger.warning("Video pipeline started for missing job pk=%s", video_pk)
        return

    try:
        client = get_videogen_client()
        _produce(client, video)
    except VideoGenJobFailedError as exc:
        logger.warning("VideoGen job failed for %s: %s", video_pk, exc)
        video.mark_failed(str(exc), code=exc.code or "")
    except VideoGenError as exc:
        logger.warning("VideoGen error for %s: %s", video_pk, exc)
        video.mark_failed(str(exc), code=getattr(exc, "code", "") or "")
    except Exception as exc:  # never leave the job stuck in a non-terminal state
        logger.exception("Unexpected error producing video %s", video_pk)
        video.mark_failed(f"Unexpected error: {exc}")
    finally:
        # This runs on its own thread; release the thread-local DB connection.
        connections.close_all()


def _produce(client: VideoGenClient, video: ArticleVideo) -> None:
    """The happy path: render -> single export -> download the MP4."""
    video.mark_processing(_RENDER_BASE)

    # 1. Start the render (workflow run). Reuse an existing run on resume.
    if not video.provider_run_id:
        run = client.create_script_to_video(
            script=video.script,
            visual_style=getattr(settings, "VIDEOGEN_VISUAL_STYLE", "STOCK"),
            aspect_width=getattr(settings, "VIDEOGEN_ASPECT_RATIO", (16, 9))[0],
            aspect_height=getattr(settings, "VIDEOGEN_ASPECT_RATIO", (16, 9))[1],
        )
        video.provider_run_id = run.get("workflowRunId", "")
        video.provider_project_id = run.get("projectId", "")
        if not video.provider_run_id or not video.provider_project_id:
            raise VideoGenError(
                "VideoGen did not return a workflow run id / project id."
            )
        video.save(
            update_fields=[
                "provider_run_id",
                "provider_project_id",
                "updated_at",
            ]
        )

    # 2. Wait for the render to finish.
    _poll(
        lambda: client.get_workflow_run(video.provider_run_id),
        video=video,
        base=_RENDER_BASE,
        span=_RENDER_SPAN,
        what="render",
    )

    # 3. Export exactly once to a 720p 16:9 MP4 (reuse on resume).
    if not video.provider_export_id:
        export = client.export_project(
            video.provider_project_id,
            quality=getattr(settings, "VIDEOGEN_EXPORT_QUALITY", "STANDARD"),
        )
        video.provider_export_id = export.get("exportId", "")
        if not video.provider_export_id:
            raise VideoGenError("VideoGen did not return an export id.")
        video.save(update_fields=["provider_export_id", "updated_at"])

    # 4. Wait for the export and capture the download location.
    final_export = _poll(
        lambda: client.get_project_export(
            video.provider_project_id, video.provider_export_id
        ),
        video=video,
        base=_EXPORT_BASE,
        span=_EXPORT_SPAN,
        what="export",
    )

    # 5. Download the finished MP4 into local storage so it is served through
    #    the site and stays available for as long as the article exists.
    video.provider_file_id = final_export.get("exportFileId") or ""
    download_url = _resolve_download_url(client, final_export, video)
    _download_mp4(client, video, download_url)

    video.mark_succeeded()
    logger.info(
        "Article video ready: job=%s page=%s bytes=%s",
        video.job_id,
        video.page_id,
        video.video_bytes,
    )


def _poll(fetch, *, video: ArticleVideo, base: int, span: int, what: str) -> dict:
    """Poll ``fetch`` until VideoGen reports a terminal status.

    Updates ``video.progress_percentage`` from the provider's per-phase
    progress. Raises :class:`VideoGenJobFailedError` on failed/cancelled and
    :class:`VideoGenTimeoutError` if the phase never finishes in time.
    """
    interval = getattr(settings, "VIDEOGEN_POLL_INTERVAL_SECONDS", 5)
    deadline = time.monotonic() + getattr(
        settings, "VIDEOGEN_RENDER_TIMEOUT_SECONDS", 30 * 60
    )

    while True:
        state = fetch()
        status = state.get("status")
        pct = state.get("progressPercentage") or 0
        video.set_progress(base + int(span * (float(pct) / 100.0)))

        if status in TERMINAL_STATUSES:
            if status == "succeeded":
                return state
            error = state.get("error") or {}
            message = (
                error.get("message") if isinstance(error, dict) else None
            ) or f"VideoGen {what} {status}."
            code = error.get("code") if isinstance(error, dict) else None
            raise VideoGenJobFailedError(message, code=code)

        if time.monotonic() > deadline:
            raise VideoGenTimeoutError(
                f"VideoGen {what} did not finish within the allotted time."
            )
        time.sleep(interval)


def _resolve_download_url(
    client: VideoGenClient, export_state: dict, video: ArticleVideo
) -> str:
    """Find a usable signed MP4 URL, hydrating the file if necessary."""
    url = export_state.get("downloadUrl")
    if url:
        return url

    # Fall back to the export file's own download source.
    file_info = export_state.get("file")
    if isinstance(file_info, dict):
        source = file_info.get("downloadSource") or {}
        if source.get("url"):
            return source["url"]

    if video.provider_file_id:
        hydrated = client.hydrate_file(video.provider_file_id)
        source = hydrated.get("downloadSource") or {}
        if source.get("url"):
            return source["url"]

    raise VideoGenError("VideoGen export succeeded but no download URL was returned.")


def _download_mp4(
    client: VideoGenClient, video: ArticleVideo, download_url: str
) -> None:
    video.set_progress(_DOWNLOAD_PROGRESS)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        written = client.download_to(download_url, tmp)
        tmp.flush()
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as fh:
            video.video_file.save(f"{video.job_id}.mp4", File(fh), save=False)
        video.video_bytes = written
        video.save(
            update_fields=[
                "video_file",
                "video_bytes",
                "provider_file_id",
                "updated_at",
            ]
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
