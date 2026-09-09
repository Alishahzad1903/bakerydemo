"""
HTTP API for producing a video from a blog article.

Mounted onto the existing Wagtail v3 preview API (``/api/v3-preview/``) and
authenticated the same way every other v3 endpoint is — a bearer API token.
Producing a video is an editorial action, so it is restricted to callers
permitted to publish the specific page.

Endpoints:

* ``POST /api/v3-preview/pages/{page_id}/video/`` — start producing a video.
* ``GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/`` — job state.
"""

from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .models import VideoJob
from .service import enqueue_job, maybe_refresh_download_url, start_or_get_job

# Every operation requires a valid bearer token, matching the rest of the v3 API.
router = Router(tags=["video"], auth=BearerTokenAuth())


class VideoJobStartResponse(Schema):
    videoJobId: str


class VideoJobStatusResponse(Schema):
    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


def _get_publishable_blog_page(request: HttpRequest, page_id: int) -> BlogPage:
    """
    Resolve a published blog article the caller is allowed to publish.

    Raises 404 if it is not a blog article, and 403 if the authenticated caller
    lacks publish permission on that specific page.
    """
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        raise Http404("No blog article matches the given page_id.")
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")
    return page


@router.post(
    "/{page_id}/video/",
    response={202: VideoJobStartResponse},
    url_name="pages_video_create",
    operation_id="pages_video_create",
    summary="Produce a video for a blog article",
)
def create_video(request: HttpRequest, page_id: int):
    page = _get_publishable_blog_page(request, page_id)
    if not getattr(settings, "VIDEOGEN_API_KEY", None):
        raise HttpError(503, "VideoGen integration is not configured.")

    job, created = start_or_get_job(page, request.user)
    if created:
        # Start production once the job row is committed. Runs asynchronously,
        # so this request returns without waiting for the video.
        transaction.on_commit(lambda: enqueue_job(job.pk))
    return 202, {"videoJobId": str(job.pk)}


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusResponse,
    url_name="pages_video_status",
    operation_id="pages_video_status",
    summary="Video production status for a blog article",
)
def get_video(request: HttpRequest, page_id: int, video_job_id: UUID):
    page = _get_publishable_blog_page(request, page_id)
    job = get_object_or_404(VideoJob, pk=video_job_id, page_id=page.pk)
    # When ready, ensure the returned download URL is (and stays) valid.
    maybe_refresh_download_url(job)
    return {
        "videoJobId": str(job.pk),
        "status": job.status,
        "progressPercentage": job.progress,
        "downloadUrl": job.download_url or None,
        "error": job.error or None,
    }
