"""HTTP API for producing a narrated video from a published blog article.

These routes are mounted onto the *existing* Wagtail v3 API instance (see
:meth:`bakerydemo.videos.apps.VideosConfig.ready`), so they live under
``/api/v3-preview/pages/{page_id}/video/`` and inherit that API's bearer-token
authentication, RFC 7807 error responses and OpenAPI documentation.

Producing a video is an editorial action on a page, so it is restricted to
callers permitted to publish *that* page.
"""

import uuid

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Schema
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import services
from .models import VideoJob, VideoJobStatus

# The whole feature requires a resolved bearer token, exactly like the v3 API's
# other write/editorial endpoints.
video_router = Router(tags=["article-video"], auth=BearerTokenAuth())

_ROUTES_REGISTERED = False


# --- response schemas -----------------------------------------------------
#
# Field names are intentionally camelCase so the JSON keys match the contract
# required by the task (``videoJobId``, ``progressPercentage``, ``downloadUrl``)
# exactly, regardless of Ninja's alias settings.


class VideoJobCreatedSchema(Schema):
    videoJobId: str


class VideoJobStatusSchema(Schema):
    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


# --- helpers --------------------------------------------------------------


def _resolve_publishable_article(request: HttpRequest, page_id: int) -> BlogPage:
    """Return the live blog article ``page_id``, or raise 404 / 403.

    404 if it is not a published blog article; 403 if the caller may not publish
    that specific page.
    """
    page = get_object_or_404(BlogPage.objects.live(), pk=page_id)
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this article.")
    return page


def _get_job(page: BlogPage, video_job_id: str) -> VideoJob:
    try:
        job_uuid = uuid.UUID(str(video_job_id))
    except (ValueError, TypeError) as exc:
        raise Http404("No video job matches the given id.") from exc
    return get_object_or_404(VideoJob, pk=job_uuid, page_id=page.pk)


def _status_payload(request: HttpRequest, job: VideoJob) -> dict:
    download_url = None
    if job.status == VideoJobStatus.READY and job.video_file:
        path = reverse(
            "wagtailapi_v3:article_video_download",
            kwargs={"page_id": job.page_id, "video_job_id": str(job.pk)},
        )
        download_url = request.build_absolute_uri(path)
    return {
        "videoJobId": str(job.pk),
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": download_url,
        "error": job.error or None,
    }


# --- endpoints ------------------------------------------------------------


@video_router.post(
    "/{page_id}/video/",
    response={202: VideoJobCreatedSchema, 200: VideoJobCreatedSchema},
    url_name="article_video_create",
    operation_id="pages_video_create",
    summary="Produce a video for an article",
)
@require_any_permission(Page, ("publish",))
def create_article_video(request: HttpRequest, page_id: int):
    """Start producing a narrated video for a published article.

    Returns 202 with the new ``videoJobId`` when work is started, or 200 with
    the existing ``videoJobId`` when a video is already in flight or produced
    for this article (so asking twice never produces — or bills for — two).
    """
    page = _resolve_publishable_article(request, page_id)
    job, created = services.start_video_job(page)
    if created:
        services.enqueue_video_job(job.pk)
        return 202, {"videoJobId": str(job.pk)}
    return 200, {"videoJobId": str(job.pk)}


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    url_name="article_video_status",
    operation_id="pages_video_status",
    summary="Get the state and outcome of a video request",
)
@require_any_permission(Page, ("publish",))
def get_article_video(request: HttpRequest, page_id: int, video_job_id: str):
    page = _resolve_publishable_article(request, page_id)
    job = _get_job(page, video_job_id)
    return _status_payload(request, job)


@video_router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="article_video_download",
    operation_id="pages_video_download",
    summary="Download the finished MP4",
)
@require_any_permission(Page, ("publish",))
def download_article_video(
    request: HttpRequest, page_id: int, video_job_id: str
):
    page = _resolve_publishable_article(request, page_id)
    job = _get_job(page, video_job_id)
    if job.status != VideoJobStatus.READY or not job.video_file:
        raise Http404("The video is not ready to download.")
    return FileResponse(
        job.video_file.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"{page.slug}.mp4",
    )


def register_video_routes(api) -> None:
    """Mount the article-video routes onto the shared Wagtail v3 API instance."""
    global _ROUTES_REGISTERED
    if _ROUTES_REGISTERED:
        return
    api.add_router("/pages/", video_router)
    _ROUTES_REGISTERED = True
