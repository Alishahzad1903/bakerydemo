"""v3 API endpoints for producing a shareable video from a blog article.

Mounted under the existing ``/api/v3-preview/`` API and following its
conventions: Django Ninja routers, ``BearerTokenAuth`` for authentication, and
RFC 7807 ``problem+json`` errors via the shared exception handlers. Producing a
video is an editorial action, so every endpoint is restricted to callers
permitted to *publish* the target page.

Endpoints (all under ``/pages/``):

* ``POST /{page_id}/video/``                      start (or return the existing) job
* ``GET  /{page_id}/video/{video_job_id}/``       job status and outcome
* ``GET  /{page_id}/video/{video_job_id}/download/`` redirect to the finished MP4
"""

from __future__ import annotations

import uuid

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import NoReverseMatch, reverse
from ninja import Router, Schema
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .models import VideoJob, VideoJobStatus
from .narration import build_narration_for_page
from .runner import start_production
from .service import refresh_download_url

router = Router(tags=["pages"], auth=BearerTokenAuth())

# Guard so registering the routes is idempotent (``AppConfig.ready`` may run in
# processes that import this module more than once).
_REGISTERED = False


class VideoJobSchema(Schema):
    """Response body. Field names are the required camelCase wire keys."""

    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


def _require_publish(request: HttpRequest, page: Page) -> None:
    """Gate the request behind permission to publish ``page`` (403/401)."""
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")


def _get_blog_page(page_id: int) -> BlogPage:
    """Return the published :class:`BlogPage` for ``page_id`` or raise HTTP error."""
    page = get_object_or_404(Page, pk=page_id)
    specific = page.specific
    if not isinstance(specific, BlogPage):
        raise HttpError(400, "Video generation is only available for blog articles.")
    if not specific.live:
        raise HttpError(400, "The article is not published.")
    return specific


def _download_endpoint_url(request: HttpRequest, job: VideoJob) -> str:
    try:
        path = reverse(
            "wagtailapi_v3:pages_video_download",
            kwargs={"page_id": job.page_id, "video_job_id": str(job.id)},
        )
    except NoReverseMatch:
        path = f"/api/v3-preview/pages/{job.page_id}/video/{job.id}/download/"
    return request.build_absolute_uri(path)


def _serialize(request: HttpRequest, job: VideoJob) -> dict:
    return {
        "videoJobId": str(job.id),
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": _download_endpoint_url(request, job) if job.is_ready else None,
        "error": job.error or None,
    }


@router.post(
    "/{page_id}/video/",
    response={202: VideoJobSchema, 200: VideoJobSchema},
    url_name="pages_video_create",
    operation_id="pages_video_create",
    summary="Produce a video for a blog article",
)
def create_video(request: HttpRequest, page_id: int):
    page = get_object_or_404(Page, pk=page_id)
    _require_publish(request, page)
    blog_page = _get_blog_page(page_id)

    # Idempotency: at most one active (non-failed) job per article. A repeat
    # request returns the existing job — it never produces or bills a second
    # video.
    existing = (
        VideoJob.objects.filter(page_id=page_id)
        .exclude(status=VideoJobStatus.FAILED)
        .first()
    )
    if existing is not None:
        return 200, _serialize(request, existing)

    script = build_narration_for_page(blog_page)
    try:
        with transaction.atomic():
            job = VideoJob.objects.create(page=page, script=script)
    except IntegrityError:
        # Lost a race against a concurrent request; return the winner's job.
        existing = (
            VideoJob.objects.filter(page_id=page_id)
            .exclude(status=VideoJobStatus.FAILED)
            .first()
        )
        if existing is not None:
            return 200, _serialize(request, existing)
        raise

    start_production(job)
    return 202, _serialize(request, job)


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobSchema,
    url_name="pages_video_status",
    operation_id="pages_video_status",
    summary="Video production status and outcome",
)
def get_video_status(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    page = get_object_or_404(Page, pk=page_id)
    _require_publish(request, page)
    job = get_object_or_404(VideoJob, pk=video_job_id, page_id=page_id)
    return _serialize(request, job)


@router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="pages_video_download",
    operation_id="pages_video_download",
    summary="Download the finished MP4",
)
def download_video(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    page = get_object_or_404(Page, pk=page_id)
    _require_publish(request, page)
    job = get_object_or_404(VideoJob, pk=video_job_id, page_id=page_id)
    if not job.is_ready:
        raise HttpError(409, "The video is not ready to download yet.")
    url = refresh_download_url(job)
    if not url:
        raise HttpError(502, "The download URL is currently unavailable.")
    return redirect(url)


def register_video_routes() -> None:
    """Register the video routes on the shared v3 NinjaAPI instance.

    Must run before ``api.urls`` is first accessed; called from
    ``VideosConfig.ready()``.
    """
    global _REGISTERED
    if _REGISTERED:
        return
    from wagtail.api.v3.api import api

    api.add_router("/pages/", router)
    _REGISTERED = True
