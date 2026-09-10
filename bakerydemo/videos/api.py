"""The two video endpoints, mounted onto the existing Wagtail v3 API.

Both live under the existing ``/api/v3-preview/pages/`` prefix, authenticate with
the same bearer-token scheme the rest of the v3 API uses, and — since producing a
video is an editorial action on a page — are restricted to callers permitted to
**publish** that page.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import service
from .exceptions import (
    VideoGenAPIError,
    VideoGenConfigError,
    VideoGenError,
    VideoGenUnavailableError,
)
from .models import VideoJob
from .schemas import StartVideoResponse, VideoStatusResponse

# Bearer auth on every operation, exactly like the rest of the v3 write API.
video_router = Router(auth=BearerTokenAuth(), tags=["videos"])


def _get_blog_page_or_404(page_id: int, *, require_live: bool) -> BlogPage:
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        # The capability is blog-only; hide it for other page types.
        raise Http404("Video generation is only available for blog articles.")
    if require_live and not page.live:
        raise HttpError(422, "Video generation requires a published article.")
    return page


def _require_publish(request: HttpRequest, page: BlogPage) -> None:
    # Per-page publish permission — the precise "permitted to publish that page" check.
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")


def _as_http_error(exc: VideoGenError) -> HttpError:
    """Map a typed provider failure onto an HTTP response (never leaking detail)."""
    if isinstance(exc, VideoGenConfigError):
        return HttpError(500, "Video generation is not configured.")
    if isinstance(exc, VideoGenUnavailableError):
        return HttpError(503, "VideoGen is temporarily unavailable; please retry.")
    if isinstance(exc, VideoGenAPIError):
        return HttpError(502, f"VideoGen returned an error (HTTP {exc.status_code}).")
    return HttpError(502, "VideoGen returned an unreadable response.")


@video_router.post(
    "/{page_id}/video/",
    response={200: StartVideoResponse, 202: StartVideoResponse},
    by_alias=True,
    url_name="pages_video_start",
    operation_id="pages_video_start",
    summary="Start producing a video for a blog article",
)
def start_video(request: HttpRequest, page_id: int):
    page = _get_blog_page_or_404(page_id, require_live=True)
    _require_publish(request, page)
    try:
        job, created = service.start_video(page)
    except VideoGenError as exc:
        raise _as_http_error(exc) from exc
    status_code = 202 if created else 200
    return status_code, SimpleNamespace(video_job_id=str(job.pk))


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoStatusResponse,
    by_alias=True,
    url_name="pages_video_status",
    operation_id="pages_video_status",
    summary="Video production status and download location",
)
def video_status(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    page = _get_blog_page_or_404(page_id, require_live=False)
    _require_publish(request, page)
    job = get_object_or_404(VideoJob, pk=video_job_id, page_id=page.pk)
    try:
        job = service.advance(job)
    except VideoGenError as exc:
        raise _as_http_error(exc) from exc
    return job.status_dto()
