"""v3 API endpoints for producing and retrieving article videos.

These are mounted onto the project's existing Wagtail v3 API (the Django-Ninja
``NinjaAPI`` served at ``/api/v3-preview/``) via :func:`register_video_endpoints`,
so they share its bearer-token authentication, its RFC 7807 error handling and
its OpenAPI docs. Endpoints:

* ``POST /api/v3-preview/pages/{page_id}/video/``
      start producing a video for the article; returns ``videoJobId``.
* ``GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/``
      the state and outcome of one request.
* ``GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/download/``
      stream the finished MP4 (referenced by the status response's ``downloadUrl``).

Producing a video is an editorial action, so every endpoint is restricted to
callers permitted to publish the target page.
"""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Schema

from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.errors import as_validation_error
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .models import VideoJob
from .services import enqueue_video_job

router = Router(tags=["videos"], auth=BearerTokenAuth())


# --- response schemas ------------------------------------------------------


class VideoJobStartSchema(Schema):
    videoJobId: str
    pageId: int
    status: str
    progressPercentage: int


class VideoJobStatusSchema(Schema):
    videoJobId: str
    pageId: int
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None
    createdAt: dt.datetime
    updatedAt: dt.datetime


# --- helpers ---------------------------------------------------------------


def _load_publishable_article(request: HttpRequest, page_id: int) -> BlogPage:
    """Resolve the target blog article and enforce publish permission.

    Order of checks mirrors the rest of the v3 API: unknown page -> 404,
    caller without publish rights on the page -> 403 (PermissionDenied), a page
    that is not a published article -> 422 validation error.
    """
    page = get_object_or_404(Page, pk=page_id).specific

    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")

    if not isinstance(page, BlogPage):
        raise as_validation_error(
            ValueError("not a blog article"),
            "Video production is only supported for blog articles.",
            loc=("page_id",),
        )
    if not page.live:
        raise as_validation_error(
            ValueError("article not published"),
            "This article is not published.",
            loc=("page_id",),
        )
    return page


def _get_job(page: Page, video_job_id: str) -> VideoJob:
    return get_object_or_404(VideoJob, pk=video_job_id, page=page)


def _download_url(request: HttpRequest, job: VideoJob) -> str | None:
    if not job.is_ready:
        return None
    path = reverse(
        "wagtailapi_v3:video_download",
        kwargs={"page_id": job.page_id, "video_job_id": str(job.id)},
    )
    return request.build_absolute_uri(path)


def _serialize_status(request: HttpRequest, job: VideoJob) -> dict:
    return {
        "videoJobId": str(job.id),
        "pageId": job.page_id,
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": _download_url(request, job),
        "error": job.error or None,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
    }


# --- endpoints -------------------------------------------------------------


@router.post(
    "/{page_id}/video/",
    response={200: VideoJobStartSchema, 202: VideoJobStartSchema},
    url_name="video_create",
    operation_id="pages_video_create",
    summary="Produce a video for an article",
)
@require_any_permission(Page, ("publish",))
def create_video(request: HttpRequest, page_id: int):
    page = _load_publishable_article(request, page_id)
    job, created = enqueue_video_job(page, request.user)
    payload = {
        "videoJobId": str(job.id),
        "pageId": job.page_id,
        "status": job.status,
        "progressPercentage": job.progress_percentage,
    }
    # 202 when a new production was started, 200 when an existing job is
    # returned (idempotent - no second video is produced).
    return (202 if created else 200), payload


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    url_name="video_status",
    operation_id="pages_video_status",
    summary="Video production status",
)
@require_any_permission(Page, ("publish",))
def get_video_status(request: HttpRequest, page_id: int, video_job_id: str):
    page = _load_publishable_article(request, page_id)
    job = _get_job(page, video_job_id)
    return _serialize_status(request, job)


@router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="video_download",
    operation_id="pages_video_download",
    summary="Download the finished video",
)
@require_any_permission(Page, ("publish",))
def download_video(request: HttpRequest, page_id: int, video_job_id: str):
    page = _load_publishable_article(request, page_id)
    job = _get_job(page, video_job_id)
    if not job.is_ready:
        raise Http404("The video for this job is not ready to download.")
    response = FileResponse(
        job.video_file.open("rb"),
        content_type="video/mp4",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="{page.slug or "article"}-video.mp4"'
    )
    return response


# --- mounting --------------------------------------------------------------

_registered = False


def register_video_endpoints(api) -> None:
    """Mount the video router onto the shared v3 ``NinjaAPI`` instance.

    Must be called before ``api.urls`` is first accessed. Safe to call more
    than once.
    """
    global _registered
    if _registered:
        return
    api.add_router("/pages/", router)
    _registered = True
