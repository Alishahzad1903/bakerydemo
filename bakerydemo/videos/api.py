"""VideoGen endpoints on the Wagtail v3 API.

Two operations, mounted under ``/pages/`` so they sit at
``/api/v3-preview/pages/{page_id}/video/`` and follow the v3 API's own
conventions (Django-Ninja router, bearer-token auth, RFC 7807 error responses).

Producing a video is an editorial action on a page, so both endpoints are
restricted to callers permitted to **publish** that page.
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
from .models import JobStatus, VideoJob
from .narration import build_narration_script

router = Router(tags=["videos"], auth=BearerTokenAuth())


# Field names are camelCase on purpose: Django-Ninja serializes responses by
# attribute name (by_alias defaults to False), so these attribute names are the
# exact JSON keys the task requires as top-level fields.
class VideoJobStartSchema(Schema):
    videoJobId: str
    pageId: int
    status: str


class VideoJobStatusSchema(Schema):
    videoJobId: str
    pageId: int
    status: str
    progressPercentage: float
    downloadUrl: str | None = None
    error: str | None = None


def _get_publishable_blog_article(request: HttpRequest, page_id: int) -> BlogPage:
    """Resolve a published blog article the caller may publish, or raise."""
    page = get_object_or_404(Page, pk=page_id).specific

    # v3 never trusts session auth; the shared PermissionDenied handler answers
    # 401 when unauthenticated and 403 otherwise.
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")

    if not isinstance(page, BlogPage):
        raise HttpError(422, "Videos can only be produced for blog articles.")
    if not page.live:
        raise HttpError(409, "Only published articles can be turned into a video.")
    return page


def _status_payload(job: VideoJob, download_url: str) -> dict:
    return {
        "videoJobId": str(job.id),
        "pageId": job.page_id,
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": (
            download_url if job.status == JobStatus.SUCCEEDED and download_url else None
        ),
        "error": (
            job.error_message
            if job.status == JobStatus.FAILED and job.error_message
            else None
        ),
    }


@router.post(
    "/{page_id}/video/",
    response={202: VideoJobStartSchema},
    url_name="pages_video_start",
    operation_id="pages_video_start",
    summary="Produce a video for a blog article",
)
def start_video(request: HttpRequest, page_id: int):
    """Start producing a narrated video for a published blog article.

    Returns immediately (production continues in the background). Asking twice
    in a row for the same article returns the same job rather than starting a
    second production.
    """
    page = _get_publishable_blog_article(request, page_id)
    script = build_narration_script(title=page.title, introduction=page.introduction)
    job, _created = service.start_or_get_job(page, script)
    return 202, {
        "videoJobId": str(job.id),
        "pageId": job.page_id,
        "status": job.status,
    }


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    url_name="pages_video_status",
    operation_id="pages_video_status",
    summary="Get the state of a video production",
)
def get_video(request: HttpRequest, page_id: int, video_job_id: str):
    """Report whether the video is still being produced, ready, or failed.

    When ready, ``downloadUrl`` carries a currently-valid signed MP4 URL; when
    failed, ``error`` carries what went wrong.
    """
    _get_publishable_blog_article(request, page_id)
    try:
        job_uuid = uuid.UUID(str(video_job_id))
    except (ValueError, TypeError) as exc:
        raise Http404("No video job matches the given id.") from exc
    job = get_object_or_404(VideoJob, pk=job_uuid, page_id=page_id)
    download_url = service.refresh_download_url(job)
    return _status_payload(job, download_url)
