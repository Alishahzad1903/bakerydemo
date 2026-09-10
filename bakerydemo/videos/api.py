"""Video endpoints for the Wagtail v3 (preview) HTTP API.

Two operations, nested under a page and following the v3 API's own conventions
(Django Ninja router, bearer-token auth, RFC 7807 error responses):

- ``POST /api/v3-preview/pages/{page_id}/video/`` — start producing a video for
  the article. Returns ``202`` with a top-level ``videoJobId``; the work
  continues in the background.
- ``GET  /api/v3-preview/pages/{page_id}/video/{video_job_id}/`` — report the
  state and outcome via top-level ``status``, ``progressPercentage``,
  ``downloadUrl`` and ``error``.

Producing a video is an editorial action, so both are restricted to callers
authenticated by the v3 API who are permitted to publish the target page.
"""

from __future__ import annotations

from uuid import UUID

import swapper
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError

from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.permissions import require_any_permission

from bakerydemo.blog.models import BlogPage

from .models import VideoJob
from .services import refresh_download_url, start_video_job

Page = swapper.load_model("wagtailcore", "Page")

video_router = Router(tags=["videos"])


class StartVideoResponse(Schema):
    """Response body for a started production request."""

    videoJobId: str


class VideoJobStatusResponse(Schema):
    """State and outcome of one video production request."""

    videoJobId: str
    pageId: int
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


def _get_article(page_id: int) -> BlogPage:
    """Fetch a published blog article, or raise the appropriate HTTP error."""
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        raise HttpError(422, "Videos can only be produced for blog articles.")
    if not page.live:
        raise HttpError(422, "Only published articles can be turned into a video.")
    return page


def _require_publish_permission(request: HttpRequest, page) -> None:
    """Ensure the caller may publish this specific page."""
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")


def _serialize(job: VideoJob) -> dict:
    return {
        "videoJobId": str(job.id),
        "pageId": job.page_id,
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": job.download_url or None,
        "error": job.error or None,
    }


@video_router.post(
    "/{page_id}/video/",
    response={202: StartVideoResponse},
    url_name="pages_video_create",
    summary="Produce a video for an article",
    operation_id="pages_video_create",
    auth=BearerTokenAuth(),
)
@require_any_permission(Page, ("publish",))
def create_page_video(request: HttpRequest, page_id: int):
    """Start (or reuse) the video production for a published blog article."""
    page = _get_article(page_id)
    _require_publish_permission(request, page)
    job = start_video_job(page)
    return 202, {"videoJobId": str(job.id)}


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusResponse,
    url_name="pages_video_detail",
    summary="Video production status",
    operation_id="pages_video_detail",
    auth=BearerTokenAuth(),
)
@require_any_permission(Page, ("publish",))
def get_page_video(request: HttpRequest, page_id: int, video_job_id: str):
    """Report the state and outcome of one production request."""
    page = get_object_or_404(Page, pk=page_id).specific
    _require_publish_permission(request, page)
    try:
        job_uuid = UUID(video_job_id)
    except (ValueError, TypeError) as exc:
        raise Http404("No video job matches the given id.") from exc
    job = get_object_or_404(VideoJob, id=job_uuid, page_id=page_id)
    job = refresh_download_url(job)
    return _serialize(job)


def register_video_routes() -> None:
    """Attach the video router to the Wagtail v3 API.

    Called from the project URLConf before ``api.urls`` is accessed. Guarded so
    that importing this module more than once does not register twice.
    """
    from wagtail.api.v3.urls import api

    if getattr(api, "_bakerydemo_video_routes_registered", False):
        return
    api.add_router("/pages/", video_router)
    api._bakerydemo_video_routes_registered = True
