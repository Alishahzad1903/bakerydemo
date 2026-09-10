"""Article-video endpoints on the existing Wagtail v3 HTTP API.

Mounted under the v3 ``/pages/`` prefix (see :mod:`bakerydemo.videos.apps`), so
the full paths are:

* ``POST   /api/v3-preview/pages/{page_id}/video/``                     — start
* ``GET    /api/v3-preview/pages/{page_id}/video/{video_job_id}/``      — status
* ``GET    /api/v3-preview/pages/{page_id}/video/{video_job_id}/download/`` — MP4

Authentication is the v3 API's own bearer-token scheme (:class:`BearerTokenAuth`),
and every endpoint is restricted to callers permitted to *publish* the page —
producing a video is an editorial action.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router
from wagtail.api.v3.auth import BearerTokenAuth

from .exceptions import VideoGenConfigurationError
from .models import ArticleVideo
from .schemas import StartVideoResponse, VideoStatusResponse
from .services import get_publishable_blog_article, start_video_for_page

# Every route on this router requires a resolved bearer token, exactly like the
# rest of the v3 write API.
router = Router(tags=["article videos"], auth=BearerTokenAuth())


def _get_article_or_404(page_id: int):
    """Resolve a live blog article by id, or 404."""
    page = get_publishable_blog_article(page_id)
    if page is None:
        raise Http404("No published blog article matches the given id.")
    return page


def _require_publish_permission(request: HttpRequest, page) -> None:
    """Restrict the action to callers who may publish this specific page."""
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")


def _download_url(request: HttpRequest, video: ArticleVideo) -> str | None:
    if not video.is_ready:
        return None
    path = reverse(
        "wagtailapi_v3:article_video_download",
        kwargs={"page_id": video.page_id, "video_job_id": str(video.job_id)},
    )
    return request.build_absolute_uri(path)


def _status_payload(request: HttpRequest, video: ArticleVideo) -> dict:
    return {
        "video_job_id": str(video.job_id),
        "status": video.status,
        "progress_percentage": video.progress_percentage,
        "download_url": _download_url(request, video),
        "error": video.error_message or None,
    }


@router.post(
    "/{page_id}/video/",
    response={202: StartVideoResponse, 200: StartVideoResponse},
    by_alias=True,
    url_name="article_video_start",
    operation_id="pages_video_start",
    summary="Produce a video for a published blog article",
)
def start_video(request: HttpRequest, page_id: int):
    """Start producing a video for the article, or return the existing job.

    Idempotent: a second call for the same article returns the same job
    (HTTP 200) without starting or billing a second render. The first call
    returns HTTP 202 with the new ``videoJobId``.
    """
    page = _get_article_or_404(page_id)
    _require_publish_permission(request, page)

    try:
        video, created = start_video_for_page(page)
    except VideoGenConfigurationError as exc:
        # Misconfiguration is a server-side problem, not a client error.
        from ninja.errors import HttpError

        raise HttpError(503, str(exc)) from exc

    payload = {
        "video_job_id": str(video.job_id),
        "status": video.status,
        "progress_percentage": video.progress_percentage,
    }
    return (202 if created else 200), payload


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoStatusResponse,
    by_alias=True,
    url_name="article_video_status",
    operation_id="pages_video_status",
    summary="Get the state and outcome of an article video job",
)
def get_video_status(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    """Report whether the video is producing, ready (with a download URL), or
    failed (with the error)."""
    page = _get_article_or_404(page_id)
    _require_publish_permission(request, page)
    video = get_object_or_404(ArticleVideo, page=page, job_id=video_job_id)
    return _status_payload(request, video)


@router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="article_video_download",
    operation_id="pages_video_download",
    summary="Download the finished MP4 for an article video",
)
def download_video(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    """Stream the finished MP4 through the site (available while the article
    exists)."""
    page = _get_article_or_404(page_id)
    _require_publish_permission(request, page)
    video = get_object_or_404(ArticleVideo, page=page, job_id=video_job_id)
    if not video.is_ready:
        raise Http404("The video is not ready for download.")

    response = FileResponse(
        video.video_file.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"{page.slug or 'article'}-video.mp4",
    )
    return response
