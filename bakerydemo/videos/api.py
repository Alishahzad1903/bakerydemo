"""Ninja endpoints for producing and tracking article videos.

These are *additive* routes mounted on the existing Wagtail v3 API under the
``/pages/`` prefix, authenticated exactly like the rest of that API
(``BearerTokenAuth``). Producing a video is an editorial action, so both routes
are restricted to callers permitted to **publish that page**.

* ``POST /api/v3-preview/pages/{page_id}/video/``
    Start producing a video. Returns immediately with ``videoJobId``.
* ``GET  /api/v3-preview/pages/{page_id}/video/{video_job_id}/``
    Report ``status`` / ``progressPercentage`` / ``downloadUrl`` / ``error``.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import service
from .exceptions import VideoGenConfigError

video_router = Router(tags=["video"])


class StartVideoResponse(Schema):
    videoJobId: str


class VideoJobStateResponse(Schema):
    videoJobId: str
    status: str
    progressPercentage: float
    downloadUrl: str | None = None
    error: str | None = None


def _get_publishable_page(request: HttpRequest, page_id: int) -> Page:
    """Resolve the page and require publish permission on *that* page."""
    page = get_object_or_404(Page, pk=page_id).specific
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")
    return page


def _require_published_article(page: Page, page_id: int) -> BlogPage:
    if not isinstance(page, BlogPage):
        raise HttpError(400, f"Page {page_id} is not a blog article.")
    if not page.live:
        raise HttpError(400, f"Page {page_id} is not a published article.")
    return page


@video_router.post(
    "/{page_id}/video/",
    response={202: StartVideoResponse},
    auth=BearerTokenAuth(),
    url_name="pages_video_start",
    operation_id="pages_video_start",
    summary="Produce a video for a blog article",
)
def start_video(request: HttpRequest, page_id: int):
    page = _get_publishable_page(request, page_id)
    article = _require_published_article(page, page_id)
    try:
        article_video = service.request_video(article)
    except VideoGenConfigError as exc:
        raise HttpError(503, "Video generation is not configured.") from exc
    return 202, {"videoJobId": str(article_video.job_id)}


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStateResponse,
    auth=BearerTokenAuth(),
    url_name="pages_video_status",
    operation_id="pages_video_status",
    summary="Get the status of an article video",
)
def get_video(request: HttpRequest, page_id: int, video_job_id: str):
    page = _get_publishable_page(request, page_id)
    try:
        job_uuid = uuid.UUID(video_job_id)
    except ValueError as exc:
        raise Http404("No such video job for this article.") from exc

    article_video = service.get_job_state(page, job_uuid)
    download_url = service.fresh_download_url(article_video)
    return {
        "videoJobId": str(article_video.job_id),
        "status": article_video.status,
        "progressPercentage": article_video.progress_percentage,
        "downloadUrl": download_url,
        "error": article_video.error or None,
    }
