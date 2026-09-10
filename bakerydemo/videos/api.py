"""v3 API endpoints for producing an article's video.

These two operations are mounted onto the site's existing ``/api/v3-preview/``
router under ``/pages/{page_id}/video/``. They follow that API's conventions:
Django-Ninja schemas, bearer-token auth, the shared RFC 7807 error handlers and
``require_any_permission`` for gating. Producing a video is an editorial action,
so both endpoints are restricted to callers permitted to publish the page.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.errors import as_validation_error
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import service
from .models import VideoJob

router = Router(tags=["video"], auth=BearerTokenAuth())


class StartVideoResponse(Schema):
    """202 response identifying the accepted work."""

    videoJobId: str


class VideoJobStatusResponse(Schema):
    """State and outcome of one video request.

    ``status`` is one of ``pending``/``processing`` (still being produced),
    ``ready`` (downloadable) or ``failed``. ``downloadUrl`` is populated only
    when ready; ``error`` only when failed.
    """

    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


def _get_publishable_article(request: HttpRequest, page_id: int) -> BlogPage:
    """Resolve a live blog article the caller may publish, or raise.

    * 404 if no page has that id.
    * 422 if the page is not a (published) blog article - videos are only
      produced for blog articles.
    * 403/401 (via ``PermissionDenied``) if the caller cannot publish it.
    """
    page = get_object_or_404(Page, pk=page_id).specific

    if not isinstance(page, BlogPage):
        raise as_validation_error(
            ValueError("Page is not a blog article."),
            "Videos can only be produced for blog articles.",
            loc=("path", "page_id"),
        )
    if not page.live:
        raise as_validation_error(
            ValueError("Page is not published."),
            "Videos can only be produced for published articles.",
            loc=("path", "page_id"),
        )
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")
    return page


def _serialize(job: VideoJob) -> dict:
    return {
        "videoJobId": str(job.id),
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": job.download_url or None,
        "error": job.error or None,
    }


@router.post(
    "/{page_id}/video/",
    response={202: StartVideoResponse},
    url_name="pages_video_start",
    operation_id="pages_video_start",
    summary="Produce a video for an article",
)
@require_any_permission(Page, ("publish",))
def start_video(request: HttpRequest, page_id: int):
    """Start producing a video for the article; returns immediately with a job id."""
    page = _get_publishable_article(request, page_id)
    job = service.request_video(page)
    return 202, {"videoJobId": str(job.id)}


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusResponse,
    url_name="pages_video_status",
    operation_id="pages_video_status",
    summary="Get the state of an article's video request",
)
@require_any_permission(Page, ("publish",))
def get_video(request: HttpRequest, page_id: int, video_job_id: str):
    """Report a job's state; when ready, re-signs and returns the MP4 download URL."""
    page = _get_publishable_article(request, page_id)
    try:
        job = service.get_job(page, video_job_id)
    except service.VideoJobNotFound as exc:
        raise Http404("No video job matches the given id for this article.") from exc
    service.refresh_download_url(job)
    return _serialize(job)
