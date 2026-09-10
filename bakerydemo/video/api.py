"""
HTTP API for producing a shareable video from a published blog article.

Mounted on the site's existing Wagtail v3 API (``/api/v3-preview/``) and
authenticated the same way (bearer API token). Producing a video is an
editorial action on a page, so it is restricted to callers permitted to
*publish* that page.

Endpoints:

* ``POST /api/v3-preview/pages/{page_id}/video/``
  Start producing a video for the article. Returns immediately with the
  top-level ``videoJobId`` identifying the work. Idempotent: asking twice in a
  row returns the same job and does not start (or bill for) a second video.

* ``GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/``
  Report the state and outcome as top-level ``status``, ``progressPercentage``,
  ``downloadUrl`` and ``error``. ``downloadUrl`` is populated once the MP4 is
  ready; ``error`` once it has failed.

* ``GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/download/``
  Stream the finished MP4 (same authentication and publish gate).
"""

from __future__ import annotations

import uuid

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import NoReverseMatch, reverse
from ninja import Field, Router, Schema
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .models import ArticleVideo
from .narration import build_script
from .service import start_async_production

router = Router(tags=["article videos"])


class StartVideoResponse(Schema):
    model_config = {"populate_by_name": True}

    video_job_id: str = Field(alias="videoJobId")


class VideoStatusResponse(Schema):
    model_config = {"populate_by_name": True}

    video_job_id: str = Field(alias="videoJobId")
    status: str
    progress_percentage: int = Field(alias="progressPercentage")
    download_url: str | None = Field(default=None, alias="downloadUrl")
    error: str | None = Field(default=None, alias="error")


def _get_publishable_article(request: HttpRequest, page_id: int) -> BlogPage:
    """
    Resolve ``page_id`` to a live blog article the caller may publish.

    * 404 if the page does not exist or is not a published blog article.
    * 401/403 (via the shared exception handlers) if the caller may not publish.
    """
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage) or not page.live:
        raise Http404("No published blog article matches the given page id.")
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")
    return page


def _download_url(request: HttpRequest, article_video: ArticleVideo) -> str | None:
    if article_video.status != ArticleVideo.Status.READY or not article_video.video_file:
        return None
    try:
        path = reverse(
            "wagtailapi_v3:article_video_download",
            kwargs={
                "page_id": article_video.page_id,
                "video_job_id": str(article_video.job_id),
            },
        )
    except NoReverseMatch:  # pragma: no cover - defensive
        return None
    return request.build_absolute_uri(path)


@router.post(
    "/pages/{page_id}/video/",
    response={202: StartVideoResponse, 200: StartVideoResponse},
    auth=BearerTokenAuth(),
    by_alias=True,
    url_name="article_video_start",
    operation_id="article_video_start",
    summary="Produce a video for a blog article",
)
def start_video(request: HttpRequest, page_id: int):
    page = _get_publishable_article(request, page_id)

    script = build_script(page.title, page.introduction)
    if not script:
        raise HttpError(
            422,
            "This article has no title or introduction text to narrate.",
        )

    article_video, created = ArticleVideo.objects.get_or_create(
        page=page,
        defaults={"script": script, "status": ArticleVideo.Status.PENDING},
    )

    if created:
        # First request for this article: start the single production run.
        start_async_production(article_video)
        return 202, {"video_job_id": str(article_video.job_id)}

    # A job already exists for this article. Return it as-is and never start a
    # second production — asking twice must not produce two videos or bill
    # twice, and a failed attempt is diagnosed (via GET), not silently retried.
    return 200, {"video_job_id": str(article_video.job_id)}


@router.get(
    "/pages/{page_id}/video/{video_job_id}/",
    response=VideoStatusResponse,
    auth=BearerTokenAuth(),
    by_alias=True,
    url_name="article_video_status",
    operation_id="article_video_status",
    summary="Get the state and outcome of an article video job",
)
def video_status(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    page = _get_publishable_article(request, page_id)
    article_video = get_object_or_404(
        ArticleVideo, page_id=page.pk, job_id=video_job_id
    )
    return {
        "video_job_id": str(article_video.job_id),
        "status": article_video.status,
        "progress_percentage": article_video.progress_percentage,
        "download_url": _download_url(request, article_video),
        "error": article_video.error or None,
    }


@router.get(
    "/pages/{page_id}/video/{video_job_id}/download/",
    auth=BearerTokenAuth(),
    url_name="article_video_download",
    operation_id="article_video_download",
    summary="Download the finished MP4 for an article video",
)
def download_video(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    page = _get_publishable_article(request, page_id)
    article_video = get_object_or_404(
        ArticleVideo, page_id=page.pk, job_id=video_job_id
    )
    if article_video.status != ArticleVideo.Status.READY or not article_video.video_file:
        raise HttpError(409, "The video is not ready to download yet.")
    response = FileResponse(
        article_video.video_file.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"{page.slug or 'article'}-{article_video.job_id}.mp4",
    )
    return response
