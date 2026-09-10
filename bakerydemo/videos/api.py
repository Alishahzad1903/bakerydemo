"""VideoGen endpoints, mounted additively on the existing Wagtail v3 API.

Routes (under ``/api/v3-preview/pages/``), following the v3 API's own conventions —
bearer-token auth and RFC 7807 error responses:

  * ``POST   /{page_id}/video/``                     start producing a video
  * ``GET    /{page_id}/video/{video_job_id}/``      state + outcome of the request
  * ``GET    /{page_id}/video/{video_job_id}/download/``  stream the finished MP4

Producing a video is an editorial action, so every route requires the caller to be able to
publish the page (coarse ``publish`` permission gate + a per-page ``can_publish()`` check),
matching the ``publish`` page action in ``wagtail/api/v3/routers/pages.py``.
"""

import uuid

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Schema, Status
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.errors import as_validation_error
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import service
from .models import VideoJob, VideoJobState

router = Router(tags=["pages"], auth=BearerTokenAuth())

# The task mandates these exact top-level JSON keys, so the response schema uses them as
# field names verbatim (rather than snake_case + aliases) to keep the wire contract obvious.


class VideoJobErrorSchema(Schema):
    code: str
    message: str


class VideoJobStatusSchema(Schema):
    videoJobId: str
    status: str  # "processing" | "ready" | "failed"
    progressPercentage: float
    downloadUrl: str | None = None
    error: VideoJobErrorSchema | None = None


def _get_blog_page_or_404(page_id: int) -> BlogPage:
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        raise Http404("No blog article matches the given page id.")
    return page


def _require_can_publish(request: HttpRequest, page: Page) -> None:
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied


def _get_job_or_404(page: Page, video_job_id: uuid.UUID) -> VideoJob:
    return get_object_or_404(VideoJob, page=page, public_id=video_job_id)


def _serialize(job: VideoJob, request: HttpRequest) -> VideoJobStatusSchema:
    download_url = None
    if job.state == VideoJobState.READY and job.mp4:
        download_url = request.build_absolute_uri(
            reverse(
                "wagtailapi_v3:pages_video_download",
                kwargs={"page_id": job.page_id, "video_job_id": str(job.public_id)},
            )
        )

    error = None
    if job.state == VideoJobState.FAILED:
        error = VideoJobErrorSchema(
            code=job.error_code or "error",
            message=job.error_message or "Video production failed.",
        )

    return VideoJobStatusSchema(
        videoJobId=str(job.public_id),
        status=job.api_status,
        progressPercentage=round(job.progress, 1),
        downloadUrl=download_url,
        error=error,
    )


@router.post(
    "/{page_id}/video/",
    response={202: VideoJobStatusSchema, 200: VideoJobStatusSchema},
    url_name="pages_video_create",
    summary="Produce a video for an article",
    operation_id="pages_video_create",
)
@require_any_permission(Page, ("publish",))
def create_video(request: HttpRequest, page_id: int):
    page = _get_blog_page_or_404(page_id)
    _require_can_publish(request, page)
    if not page.live:
        raise as_validation_error(
            ValueError("The article must be published before a video can be produced."),
            "The article must be published before a video can be produced.",
            loc=("page_id",),
        )

    job, created = service.start_or_get_job(page)
    payload = _serialize(job, request)
    return Status(202 if created else 200, payload)


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    url_name="pages_video_status",
    summary="Video production status",
    operation_id="pages_video_status",
)
@require_any_permission(Page, ("publish",))
def get_video(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    page = _get_blog_page_or_404(page_id)
    _require_can_publish(request, page)
    job = _get_job_or_404(page, video_job_id)
    service.reconcile(job)
    return _serialize(job, request)


@router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="pages_video_download",
    summary="Download the produced MP4",
    operation_id="pages_video_download",
)
@require_any_permission(Page, ("publish",))
def download_video(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    page = _get_blog_page_or_404(page_id)
    _require_can_publish(request, page)
    job = _get_job_or_404(page, video_job_id)
    if job.state != VideoJobState.READY or not job.mp4:
        raise Http404("The video is not ready to download.")
    return FileResponse(
        job.mp4.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"article-{page_id}.mp4",
    )


_registered = False


def register_video_router() -> None:
    """Mount the video router on the Wagtail v3 API (idempotent)."""
    global _registered
    if _registered:
        return
    from wagtail.api.v3.api import api

    api.add_router("/pages/", router)
    _registered = True
