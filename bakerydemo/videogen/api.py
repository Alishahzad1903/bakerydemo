"""HTTP surface for the article-to-video flow.

These routes extend the existing Wagtail v3 API (mounted at
``/api/v3-preview/``). They follow that API's conventions: django-ninja routers,
``Authorization: Bearer <token>`` authentication via :class:`BearerTokenAuth`,
and RFC 7807 ``application/problem+json`` errors from the shared exception
handlers.

Producing a video is an editorial action, so every route additionally requires
the caller to have permission to *publish* the target page.

Routes (all under ``/api/v3-preview/pages/``):

* ``POST   /{page_id}/video/``                       - start producing a video
* ``GET    /{page_id}/video/{video_job_id}/``        - poll status / outcome
* ``GET    /{page_id}/video/{video_job_id}/download/`` - download the MP4
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Schema
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .models import VideoJob
from .service import advance_job, start_job

router = Router(tags=["videogen"], auth=BearerTokenAuth())

_DOWNLOAD_URL_NAME = "videogen_download"


# -- Schemas ---------------------------------------------------------------


class VideoJobCreatedSchema(Schema):
    videoJobId: int


class VideoJobStatusSchema(Schema):
    videoJobId: int
    # One of "processing", "ready", "failed".
    status: str
    progressPercentage: int
    # Present only once the video is ready to download.
    downloadUrl: str | None = None
    # Present only when the job failed.
    error: str | None = None


# -- Helpers ---------------------------------------------------------------


def _get_publishable_article(request: HttpRequest, page_id: int) -> BlogPage:
    """Resolve a published blog article the caller may publish, or raise.

    * 404 if no page with that id exists.
    * 403 if the caller cannot publish the page.
    * 404 if the page is not a live (published) blog article.
    """
    page = get_object_or_404(Page, pk=page_id).specific

    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")

    if not isinstance(page, BlogPage) or not page.live:
        raise Http404("No published blog article matches the given page_id.")

    return page


def _get_job(page: BlogPage, video_job_id: int) -> VideoJob:
    return get_object_or_404(VideoJob, pk=video_job_id, page_id=page.pk)


def _serialize_status(request: HttpRequest, job: VideoJob) -> dict:
    download_url = None
    if job.is_ready:
        download_url = request.build_absolute_uri(
            reverse(
                f"wagtailapi_v3:{_DOWNLOAD_URL_NAME}",
                kwargs={"page_id": job.page_id, "video_job_id": job.pk},
            )
        )
    return {
        "videoJobId": job.pk,
        "status": job.public_status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": download_url,
        "error": job.error or None,
    }


# -- Routes ----------------------------------------------------------------


@router.post(
    "/{page_id}/video/",
    response={202: VideoJobCreatedSchema},
    url_name="videogen_create",
    summary="Produce a video for a blog article",
    operation_id="pages_video_create",
)
def create_video(request: HttpRequest, page_id: int):
    """Start producing a short narrated video for the article.

    Idempotent: asking twice for the same article returns the same job without
    producing (or billing) a second video. Returns immediately with the job id;
    generation continues asynchronously.
    """
    page = _get_publishable_article(request, page_id)
    job = start_job(page)
    return 202, {"videoJobId": job.pk}


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    url_name="videogen_status",
    summary="Get the state and outcome of a video job",
    operation_id="pages_video_status",
)
def video_status(request: HttpRequest, page_id: int, video_job_id: int):
    """Report whether the video is processing, ready, or failed.

    Polling this endpoint also advances the job (VideoGen has no push callback
    configured here), so callers should poll until ``status`` is terminal.
    """
    page = _get_publishable_article(request, page_id)
    job = _get_job(page, video_job_id)
    advance_job(job)
    return _serialize_status(request, job)


@router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name=_DOWNLOAD_URL_NAME,
    summary="Download the finished MP4",
    operation_id="pages_video_download",
    include_in_schema=True,
)
def video_download(request: HttpRequest, page_id: int, video_job_id: int):
    """Stream the finished MP4 from the site's own storage.

    Served from local storage so the video stays downloadable for as long as the
    article exists, independent of the provider's expiring signed URLs.
    """
    page = _get_publishable_article(request, page_id)
    job = _get_job(page, video_job_id)
    if not job.is_ready:
        raise Http404("The video for this job is not ready to download.")
    return FileResponse(
        job.video_file.open("rb"),
        as_attachment=True,
        filename=f"{page.slug}.mp4",
        content_type="video/mp4",
    )


# -- Registration ----------------------------------------------------------

_registered = False


def register_video_routes(api) -> None:
    """Attach the video routes to the shared v3 ``api`` under ``/pages/``.

    Must run before ``api.urls`` is accessed. Guarded so repeated imports (e.g.
    in tests) do not double-register.
    """
    global _registered
    if _registered:
        return
    api.add_router("/pages/", router)
    _registered = True
