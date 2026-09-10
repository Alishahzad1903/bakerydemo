"""
Article-video endpoints, mounted onto the existing Wagtail v3 preview API.

Routes (all under ``/api/v3-preview/pages/``):

* ``POST /{page_id}/video/``                    – start producing a video.
* ``GET  /{page_id}/video/{video_job_id}/``     – state & outcome of a request.
* ``GET  /{page_id}/video/{video_job_id}/download/`` – download the finished MP4.

They follow the v3 API's own conventions: Django Ninja routers, bearer-token
authentication (:class:`BearerTokenAuth`), and RFC 7807 ``problem+json`` error
responses. Producing a video is an editorial action, so every route is gated on
the caller's permission to **publish that specific page**.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Status
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.errors import problem_response
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .models import VideoJob
from .schemas import VideoJobCreatedSchema, VideoJobStatusSchema
from .services import reconcile_job, start_video_job
from .videogen import VideoGenError

video_router = Router(tags=["article videos"], auth=BearerTokenAuth())


def _get_publishable_blog_page(request: HttpRequest, page_id: int) -> BlogPage:
    """Resolve a page for video production, enforcing permission and type.

    * 404 if the page does not exist.
    * 403 if the caller may not publish this page (authentication itself is
      already enforced by :class:`BearerTokenAuth`, which 401s anonymous or
      revoked/inactive callers before the view runs).
    * 422 if the page is not a *published* blog article.
    """
    page = get_object_or_404(Page, pk=page_id).specific

    # Producing a video is an editorial action – gate it on per-page publish
    # permission, exactly like the built-in publish action.
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied(
            "You do not have permission to publish this page."
        )

    if not isinstance(page, BlogPage):
        raise HttpError(422, "Videos can only be produced for blog articles.")
    if not page.live:
        raise HttpError(422, "Only published articles can be turned into a video.")
    return page


def _status_payload(
    request: HttpRequest, page_id: int, job: VideoJob
) -> VideoJobStatusSchema:
    download_url = None
    if job.is_ready:
        download_url = request.build_absolute_uri(
            reverse(
                "wagtailapi_v3:video_download",
                kwargs={"page_id": page_id, "video_job_id": str(job.id)},
            )
        )
    return VideoJobStatusSchema(
        videoJobId=str(job.id),
        status=job.status,
        progressPercentage=job.progress_percentage,
        downloadUrl=download_url,
        error=job.error or None,
    )


@video_router.post(
    "/{page_id}/video/",
    response={201: VideoJobCreatedSchema, 200: VideoJobCreatedSchema},
    url_name="video_create",
    summary="Produce a video for an article",
    operation_id="pages_video_create",
)
def create_article_video(request: HttpRequest, page_id: int):
    """Start producing a narrated video for a published blog article.

    Returns immediately – the video is produced asynchronously. Asking twice in
    a row for the same article returns the existing job (``200``) rather than
    producing (or billing for) a second video.
    """
    page = _get_publishable_blog_page(request, page_id)
    job, created = start_video_job(page)
    payload = VideoJobCreatedSchema(videoJobId=str(job.id))
    return Status(201 if created else 200, payload)


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    url_name="video_status",
    summary="Get the state and outcome of a video request",
    operation_id="pages_video_status",
)
def get_article_video(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    """Report whether the video is still being produced, ready, or failed.

    Each call reconciles the job against VideoGen's own reported state, so a
    caller can poll this endpoint to follow a request through to completion.
    """
    page = _get_publishable_blog_page(request, page_id)
    job = get_object_or_404(VideoJob, pk=video_job_id, page=page)
    job = reconcile_job(job)
    return _status_payload(request, page_id, job)


@video_router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="video_download",
    summary="Download the finished MP4",
    operation_id="pages_video_download",
    include_in_schema=True,
)
def download_article_video(
    request: HttpRequest, page_id: int, video_job_id: uuid.UUID
):
    """Stream the finished MP4 from the site's own storage.

    Available for as long as the article (and therefore the job) exists.
    """
    page = _get_publishable_blog_page(request, page_id)
    job = get_object_or_404(VideoJob, pk=video_job_id, page=page)
    if not job.is_ready:
        raise Http404("The video for this article is not ready to download.")
    return FileResponse(
        job.video_file.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"{page.slug}.mp4",
    )


def _videogen_error_handler(request: HttpRequest, exc: VideoGenError):
    """Surface an unrecoverable provider failure as a 502 problem response."""
    return problem_response(
        status=502,
        title="Video provider error",
        detail=str(exc.message),
    )


def register_video_routes(api) -> None:
    """Attach the article-video routes and error handling to ``api``.

    Idempotent: safe to call more than once (only the first call mutates the
    API instance).
    """
    if getattr(api, "_bakerydemo_video_registered", False):
        return
    api.add_router("/pages/", video_router)
    api.exception_handler(VideoGenError)(_videogen_error_handler)
    api._bakerydemo_video_registered = True
