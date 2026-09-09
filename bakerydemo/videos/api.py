"""VideoGen article-video endpoints on the v3-preview (Django Ninja) API.

Routes (mounted under ``/api/v3-preview/pages/``):

* ``POST /{page_id}/video/`` — start producing a video; returns ``videoJobId``.
* ``GET  /{page_id}/video/{video_job_id}/`` — status / outcome.
* ``GET  /{page_id}/video/{video_job_id}/download/`` — the finished MP4.

All three require a caller permitted to publish the page, authenticated the way
the rest of the v3 API authenticates (``BearerTokenAuth``).
"""

from __future__ import annotations

import functools
import uuid

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import runner
from .constants import VideoStatus
from .models import ArticleVideo
from .narration import build_narration
from .schemas import StartVideoResponse, VideoStatusResponse

router = Router(auth=BearerTokenAuth(), tags=["video"])

_registered = False


def register_routes() -> None:
    """Attach the video router to the shared v3-preview API (idempotent)."""
    global _registered
    if _registered:
        return
    from wagtail.api.v3.urls import api as v3_api

    v3_api.add_router("/pages/", router)
    _registered = True


# -- helpers ---------------------------------------------------------------


def _get_publishable_blog_page(request: HttpRequest, page_id: int) -> BlogPage:
    """Resolve a live blog article the caller may publish, or raise."""
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        raise HttpError(400, "Video generation is only available for blog articles.")
    if not page.live:
        raise HttpError(400, "Video generation is only available for published articles.")
    # Precise, per-page publish check (mirrors the pages publish action).
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied
    return page


def _get_job(page: Page, video_job_id: str) -> ArticleVideo:
    try:
        job_uuid = uuid.UUID(str(video_job_id))
    except (ValueError, AttributeError, TypeError):
        raise Http404("Video job not found.") from None
    return get_object_or_404(ArticleVideo, page=page, job_id=job_uuid)


def _status_payload(request: HttpRequest, article_video: ArticleVideo) -> VideoStatusResponse:
    download_url = None
    if article_video.status == VideoStatus.READY and article_video.video_file:
        path = reverse(
            "wagtailapi_v3:video_download",
            kwargs={
                "page_id": article_video.page_id,
                "video_job_id": str(article_video.job_id),
            },
        )
        download_url = request.build_absolute_uri(path)
    return VideoStatusResponse(
        videoJobId=str(article_video.job_id),
        status=article_video.status,
        progressPercentage=round(article_video.progress_percentage, 2),
        downloadUrl=download_url,
        error=article_video.error_message or None,
    )


def _get_or_start_job(page: Page) -> ArticleVideo:
    """Return the page's video job, starting production only when needed.

    Idempotent: an existing pending/processing/ready job is reused as-is (no new
    VideoGen call, no double billing). A previously failed job is reset and
    re-run in place. The background thread is started only after the row is
    committed.
    """
    with transaction.atomic():
        article_video, created = ArticleVideo.objects.get_or_create(page=page)
        should_start = created
        if not created and article_video.status == VideoStatus.FAILED:
            article_video.reset_for_retry()
            should_start = True
        if should_start:
            transaction.on_commit(
                functools.partial(runner.start_in_background, article_video.pk)
            )
    return article_video


# -- routes ----------------------------------------------------------------


@router.post(
    "/{page_id}/video/",
    response={202: StartVideoResponse},
    url_name="video_create",
    operation_id="pages_video_create",
    summary="Produce a video for a blog article",
)
@require_any_permission(Page, ("publish",))
def create_video(request: HttpRequest, page_id: int):
    page = _get_publishable_blog_page(request, page_id)
    # Validate up front that the article has its own text to narrate, so we
    # never start a billable production doomed to fail.
    if not build_narration(page):
        raise HttpError(400, "The article has no narratable text to build a video from.")
    article_video = _get_or_start_job(page)
    return 202, StartVideoResponse(videoJobId=str(article_video.job_id))


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoStatusResponse,
    url_name="video_status",
    operation_id="pages_video_status",
    summary="Get the status of an article video",
)
@require_any_permission(Page, ("publish",))
def video_status(request: HttpRequest, page_id: int, video_job_id: str):
    page = _get_publishable_blog_page(request, page_id)
    article_video = _get_job(page, video_job_id)
    return _status_payload(request, article_video)


@router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="video_download",
    operation_id="pages_video_download",
    summary="Download the finished article video (MP4)",
)
@require_any_permission(Page, ("publish",))
def video_download(request: HttpRequest, page_id: int, video_job_id: str):
    page = _get_publishable_blog_page(request, page_id)
    article_video = _get_job(page, video_job_id)
    if article_video.status != VideoStatus.READY or not article_video.video_file:
        raise Http404("The video is not ready to download.")
    return FileResponse(
        article_video.video_file.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"{page.slug or 'article'}-video.mp4",
    )
