"""
Article-video endpoints, mounted onto the existing Wagtail v3 HTTP API.

Exposed under ``/api/v3-preview/`` following that API's conventions:

* ``POST /pages/{page_id}/video/``               -> start producing a video
* ``GET  /pages/{page_id}/video/{videoJobId}/``  -> job state and outcome
* ``GET  /pages/{page_id}/video/{videoJobId}/download/`` -> stream the MP4

Callers authenticate with the same bearer API tokens the v3 API already uses,
and producing/inspecting a video is restricted to callers permitted to publish
the page (an editorial action).
"""

from __future__ import annotations

import uuid
from typing import Optional

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import FileResponse, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Schema
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .models import ArticleVideo, VideoStatus
from .narration import build_narration
from .production import start_production_async

router = Router(tags=["article videos"], auth=BearerTokenAuth())


# -- response schemas (camelCase field names are the documented contract) -----


class StartVideoResponse(Schema):
    videoJobId: str


class VideoStatusResponse(Schema):
    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: Optional[str] = None
    error: Optional[str] = None


# -- helpers ------------------------------------------------------------------


def _get_page_or_404(page_id: int) -> Page:
    return get_object_or_404(Page, pk=page_id).specific


def _require_publish_permission(request: HttpRequest, page: Page) -> None:
    """Producing/inspecting a video is restricted to publishers of the page."""
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied


def _require_blog_article(page: Page) -> BlogPage:
    if not isinstance(page, BlogPage):
        raise HttpError(422, "This page is not a blog article.")
    if not page.live:
        raise HttpError(422, "This blog article is not published.")
    return page


def _download_url(request: HttpRequest, article_video: ArticleVideo) -> Optional[str]:
    if not article_video.is_ready:
        return None
    path = reverse(
        "wagtailapi_v3:article_video_download",
        kwargs={
            "page_id": article_video.page_id,
            "video_job_id": str(article_video.job_id),
        },
    )
    return request.build_absolute_uri(path)


def _status_payload(request: HttpRequest, av: ArticleVideo) -> VideoStatusResponse:
    return VideoStatusResponse(
        videoJobId=str(av.job_id),
        status=av.status,
        progressPercentage=av.progress_percentage,
        downloadUrl=_download_url(request, av),
        error=av.error or None,
    )


# -- endpoints ----------------------------------------------------------------


@router.post(
    "/pages/{page_id}/video/",
    response={200: StartVideoResponse, 202: StartVideoResponse},
    url_name="article_video_create",
    summary="Produce a video for a blog article",
    operation_id="pages_video_create",
)
def create_article_video(request: HttpRequest, page_id: int):
    page = _get_page_or_404(page_id)
    _require_publish_permission(request, page)
    article = _require_blog_article(page)

    # Idempotency: reuse the current non-failed video for this article instead of
    # producing (and billing for) a second one. The partial unique constraint on
    # ArticleVideo is the race-proof backstop behind this check.
    created = False
    try:
        with transaction.atomic():
            existing = (
                ArticleVideo.objects.select_for_update()
                .filter(page=article)
                .exclude(status=VideoStatus.FAILED)
                .order_by("-created_at")
                .first()
            )
            if existing is not None:
                av = existing
            else:
                script = build_narration(article)
                if not script:
                    raise HttpError(422, "This article has no text to narrate.")
                av = ArticleVideo.objects.create(
                    page=article,
                    status=VideoStatus.PENDING,
                    script=script,
                    created_by=request.user if request.user.is_authenticated else None,
                )
                created = True
    except IntegrityError:
        # A concurrent request created the active row first; return that one.
        av = (
            ArticleVideo.objects.filter(page=article)
            .exclude(status=VideoStatus.FAILED)
            .order_by("-created_at")
            .first()
        )
        created = False

    if created:
        start_production_async(av)
        return 202, StartVideoResponse(videoJobId=str(av.job_id))
    return 200, StartVideoResponse(videoJobId=str(av.job_id))


@router.get(
    "/pages/{page_id}/video/{video_job_id}/",
    response=VideoStatusResponse,
    url_name="article_video_detail",
    summary="Get the state and outcome of an article video",
    operation_id="pages_video_detail",
)
def get_article_video(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    page = _get_page_or_404(page_id)
    _require_publish_permission(request, page)
    av = get_object_or_404(ArticleVideo, page_id=page.pk, job_id=video_job_id)
    return _status_payload(request, av)


@router.get(
    "/pages/{page_id}/video/{video_job_id}/download/",
    url_name="article_video_download",
    summary="Download the finished MP4 for an article video",
    operation_id="pages_video_download",
)
def download_article_video(
    request: HttpRequest, page_id: int, video_job_id: uuid.UUID
):
    page = _get_page_or_404(page_id)
    _require_publish_permission(request, page)
    av = get_object_or_404(ArticleVideo, page_id=page.pk, job_id=video_job_id)
    if not av.is_ready:
        raise HttpError(409, "The video is not ready to download yet.")

    filename = f"{page.slug or 'article'}-video.mp4"
    response = FileResponse(
        av.video_file.open("rb"),
        as_attachment=True,
        filename=filename,
        content_type="video/mp4",
    )
    return response


# -- registration -------------------------------------------------------------

_registered = False


def register_routes() -> None:
    """Attach this router to the shared Wagtail v3 ``NinjaAPI`` instance.

    Called from ``VideosAppConfig.ready()`` — before the root URLconf accesses
    ``api.urls``, which is when Django Ninja freezes its router configuration.
    """
    global _registered
    if _registered:
        return
    from wagtail.api.v3.api import api

    api.add_router("/", router)
    _registered = True
