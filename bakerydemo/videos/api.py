"""HTTP API for producing a video from a published article.

Mounted on Wagtail's existing v3 API (``/api/v3-preview/``), following its
conventions: Django Ninja routers, ``BearerTokenAuth`` authentication, and
RFC 7807 ``application/problem+json`` errors. Producing a video is an editorial
action on a page, so every endpoint is restricted to callers permitted to
publish that page.

Endpoints (relative to ``/api/v3-preview/``):

* ``POST   pages/{page_id}/video/``                     — start producing a video
* ``GET    pages/{page_id}/video/{video_job_id}/``      — job state and outcome
* ``GET    pages/{page_id}/video/{video_job_id}/download/`` — the finished MP4
"""

from __future__ import annotations

from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpRequest
from django.urls import reverse
from ninja import Router, Schema
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth

from . import services
from .models import VideoJob

router = Router(tags=["article videos"])


# ---------------------------------------------------------------------------
# Response schemas (top-level fields are camelCase, as required)
# ---------------------------------------------------------------------------


class VideoJobStartSchema(Schema):
    videoJobId: str
    status: str
    progressPercentage: int


class VideoJobStatusSchema(Schema):
    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_article(page_id: int, *, require_live: bool):
    """Resolve the blog article for ``page_id`` or raise an API error."""
    try:
        if require_live:
            return services.resolve_article(page_id)
        return services.resolve_article_any(page_id)
    except services.NotAnArticleError as exc:
        raise HttpError(400, str(exc)) from exc


def _require_publish_permission(request: HttpRequest, page) -> None:
    """Only callers permitted to publish the page may produce/read its video."""
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied(
            "You do not have permission to produce videos for this page."
        )


def _download_url(request: HttpRequest, job: VideoJob) -> str | None:
    if not job.is_ready:
        return None
    path = reverse(
        "wagtailapi_v3:article_video_download",
        kwargs={"page_id": job.page_id, "video_job_id": job.pk},
    )
    return request.build_absolute_uri(path)


def _status_payload(request: HttpRequest, job: VideoJob) -> dict:
    return {
        "videoJobId": str(job.pk),
        "status": job.public_status,
        "progressPercentage": int(job.progress_percentage),
        "downloadUrl": _download_url(request, job),
        "error": job.error_message or None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{page_id}/video/",
    response={200: VideoJobStartSchema, 202: VideoJobStartSchema},
    auth=BearerTokenAuth(),
    url_name="article_video_create",
    operation_id="article_video_create",
    summary="Produce a video for an article",
)
def create_article_video(request: HttpRequest, page_id: int):
    """Start producing a video for the article, or return the in-flight one.

    Returns ``202 Accepted`` when a new video is started and ``200 OK`` when an
    existing (in-progress or finished) video is returned instead — asking twice
    never produces or bills for a second video.
    """
    page = _get_article(page_id, require_live=True)
    _require_publish_permission(request, page)

    job, created = services.request_video(page, request.user)
    payload = {
        "videoJobId": str(job.pk),
        "status": job.public_status,
        "progressPercentage": int(job.progress_percentage),
    }
    return (202 if created else 200), payload


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    auth=BearerTokenAuth(),
    url_name="article_video_detail",
    operation_id="article_video_detail",
    summary="Video production status",
)
def get_article_video(request: HttpRequest, page_id: int, video_job_id: UUID):
    """Report whether the video is still processing, ready, or failed."""
    page = _get_article(page_id, require_live=False)
    _require_publish_permission(request, page)

    job = services.get_job_for_page(page_id, video_job_id)
    return _status_payload(request, job)


@router.get(
    "/{page_id}/video/{video_job_id}/download/",
    auth=BearerTokenAuth(),
    url_name="article_video_download",
    operation_id="article_video_download",
    summary="Download the finished video",
)
def download_article_video(request: HttpRequest, page_id: int, video_job_id: UUID):
    """Stream the finished MP4 from the site's own storage."""
    page = _get_article(page_id, require_live=False)
    _require_publish_permission(request, page)

    job = services.get_job_for_page(page_id, video_job_id)
    if not job.is_ready:
        raise HttpError(409, "The video for this article is not ready to download.")

    response = FileResponse(
        job.video_file.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"{page.slug or 'article'}-video.mp4",
    )
    return response
