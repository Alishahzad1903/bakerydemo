"""HTTP endpoints for producing and retrieving an article's video.

These are additive operations on Wagtail's existing v3 API. They follow that
API's conventions exactly: a django-ninja ``Router`` registered on the shared
``api`` instance, ``BearerTokenAuth`` for authentication, and RFC 7807
``application/problem+json`` error responses via the v3 exception handlers.

Endpoints (mounted under ``/api/v3-preview/``):

* ``POST /pages/{page_id}/video/``                     - start production
* ``GET  /pages/{page_id}/video/{video_job_id}/``      - status & outcome
* ``GET  /pages/{page_id}/video/{video_job_id}/download/`` - fetch the MP4

Producing a video is an editorial action, so every endpoint requires a caller
who is permitted to *publish the target page*.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import services
from .client import VideoGenClient
from .exceptions import VideoGenConfigurationError, VideoGenError
from .models import ArticleVideo
from .narration import build_narration_script
from .schemas import VideoJobCreateResponse, VideoJobStatusResponse

router = Router(tags=["page videos"], auth=BearerTokenAuth())


def _get_publishable_article(request, page_id: int) -> BlogPage:
    """Resolve a live blog article the caller may publish, or raise.

    Raises ``Http404`` (404) when the page does not exist, ``PermissionDenied``
    (401/403 via the v3 handler) when the caller cannot publish it, and
    ``HttpError`` (400) when the page is not a published blog article.
    """
    page = get_object_or_404(Page, pk=page_id).specific

    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")

    if not isinstance(page, BlogPage):
        raise HttpError(400, "Video generation is only available for blog articles.")
    if not page.live:
        raise HttpError(
            400, "Only published blog articles can be turned into a video."
        )
    return page


def _create_payload(video: ArticleVideo) -> dict:
    return {
        "videoJobId": str(video.id),
        "pageId": video.page_id,
        "status": video.status,
    }


def _status_payload(request, video: ArticleVideo) -> dict:
    download_url = None
    if video.is_ready:
        download_url = request.build_absolute_uri(
            reverse(
                "wagtailapi_v3:page_video_download",
                kwargs={"page_id": video.page_id, "video_job_id": str(video.id)},
            )
        )
    return {
        "videoJobId": str(video.id),
        "pageId": video.page_id,
        "status": video.status,
        "progressPercentage": video.progress_percentage,
        "downloadUrl": download_url,
        "error": video.error or None,
    }


@router.post(
    "/pages/{page_id}/video/",
    response={201: VideoJobCreateResponse, 200: VideoJobCreateResponse},
    url_name="page_video_create",
    summary="Produce a video for an article",
    operation_id="pages_video_create",
)
def create_page_video(request, page_id: int):
    """Start (or return the existing) video production for an article.

    Idempotent: the one-to-one page relation means asking twice returns the
    same job (200) and never starts a second, billed production. A newly
    started job returns 201.
    """
    page = _get_publishable_article(request, page_id)

    # Fail fast on misconfiguration so we don't create a job we can't run.
    try:
        VideoGenClient.from_settings()
    except VideoGenConfigurationError as exc:
        raise HttpError(503, "Video generation is not configured.") from exc

    try:
        video, created = ArticleVideo.objects.get_or_create(
            page=page,
            defaults={
                "script": build_narration_script(page),
                "status": ArticleVideo.Status.PENDING,
            },
        )
    except IntegrityError:
        # Lost a create race with a concurrent request; return the winner.
        video, created = ArticleVideo.objects.get(page=page), False

    if created:
        services.start_video_production(video.id)
        return 201, _create_payload(video)
    return 200, _create_payload(video)


@router.get(
    "/pages/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusResponse,
    url_name="page_video_detail",
    summary="Get the state and outcome of a video production",
    operation_id="pages_video_detail",
)
def get_page_video(request, page_id: int, video_job_id: uuid.UUID):
    page = _get_publishable_article(request, page_id)
    video = get_object_or_404(ArticleVideo, pk=video_job_id, page=page)
    return _status_payload(request, video)


@router.get(
    "/pages/{page_id}/video/{video_job_id}/download/",
    url_name="page_video_download",
    summary="Download the finished MP4",
    operation_id="pages_video_download",
    include_in_schema=True,
)
def download_page_video(request, page_id: int, video_job_id: uuid.UUID):
    """Redirect to a freshly signed download URL for the finished MP4.

    Serving the download through the site keeps the MP4 retrievable for as long
    as the article exists: the provider re-signs the URL on each read, so the
    link never goes stale from the caller's point of view.
    """
    page = _get_publishable_article(request, page_id)
    video = get_object_or_404(ArticleVideo, pk=video_job_id, page=page)

    if not video.is_ready:
        raise HttpError(409, "The video is not ready to download yet.")

    try:
        url = services.get_fresh_download_url(video) or video.download_url
    except VideoGenError:
        # Provider unreachable: fall back to the last cached signed URL.
        url = video.download_url

    if not url:
        raise HttpError(502, "The finished video is temporarily unavailable.")
    return HttpResponseRedirect(url)


def register(api) -> None:
    """Register the page-video router on the shared v3 ``api`` instance."""
    api.add_router("/", router)
