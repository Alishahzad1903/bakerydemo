"""HTTP API for producing and retrieving article videos.

These endpoints extend the site's existing Wagtail v3 API (mounted at
``/api/v3-preview/``). They reuse that API's Django-Ninja stack, its
``Authorization: Bearer <token>`` authentication and its RFC 7807 error
responses, so they behave exactly like the built-in page endpoints.

Producing a video is an editorial action: every endpoint is restricted to
callers who are permitted to *publish* the target page.

Routes (relative to ``/api/v3-preview/``):

* ``POST   /pages/{page_id}/video/``                    – start production
* ``GET    /pages/{page_id}/video/{video_job_id}/``     – status / outcome
* ``GET    /pages/{page_id}/video/{video_job_id}/download/`` – the MP4
"""

from __future__ import annotations

import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import service
from .models import VideoJob

video_router = Router(tags=["article videos"], auth=BearerTokenAuth())


def _get_publishable_blog_article(request: HttpRequest, page_id: int) -> BlogPage:
    """Load a blog article the caller is allowed to publish, or raise.

    * 404 if there is no page with that id, or it is not a blog article.
    * 401/403 (via ``PermissionDenied``) if the caller cannot publish it.
    """
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        raise Http404("No blog article matches the given id.")
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")
    return page


def _get_job(page, video_job_id: str) -> VideoJob:
    try:
        return VideoJob.objects.get(pk=uuid.UUID(str(video_job_id)), page=page)
    except (VideoJob.DoesNotExist, ValidationError, ValueError) as exc:
        raise Http404("No video job matches the given id.") from exc


def _download_url(request: HttpRequest, job: VideoJob) -> str | None:
    if not job.has_video:
        return None
    path = reverse(
        "wagtailapi_v3:video_download",
        kwargs={"page_id": job.page_id, "video_job_id": str(job.pk)},
    )
    return request.build_absolute_uri(path)


def _job_representation(request: HttpRequest, job: VideoJob) -> dict:
    return {
        "videoJobId": str(job.pk),
        "status": job.api_status,
        "progressPercentage": int(job.progress_percentage),
        "downloadUrl": _download_url(request, job),
        "error": job.error or None,
    }


@video_router.post(
    "/{page_id}/video/",
    url_name="video_create",
    summary="Produce a video for a blog article",
    operation_id="pages_video_create",
)
def create_video(request: HttpRequest, page_id: int):
    page = _get_publishable_blog_article(request, page_id)
    if not page.live:
        raise HttpError(
            409,
            "This blog article is not published; publish it before requesting a video.",
        )

    job, created = service.request_video(page)
    # 202 Accepted when a new production was started; 200 when an existing job
    # was returned unchanged (idempotent re-request).
    return JsonResponse({"videoJobId": str(job.pk)}, status=202 if created else 200)


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    url_name="video_detail",
    summary="Get the state and outcome of an article video job",
    operation_id="pages_video_detail",
)
def get_video(request: HttpRequest, page_id: int, video_job_id: str):
    page = _get_publishable_blog_article(request, page_id)
    job = _get_job(page, video_job_id)
    return _job_representation(request, job)


@video_router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="video_download",
    summary="Download the finished MP4 for an article video job",
    operation_id="pages_video_download",
)
def download_video(request: HttpRequest, page_id: int, video_job_id: str):
    page = _get_publishable_blog_article(request, page_id)
    job = _get_job(page, video_job_id)
    if not job.has_video:
        raise Http404("The video is not ready to download.")
    response = FileResponse(
        job.video_file.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"{page.slug or 'article'}-video.mp4",
    )
    return response


def register_video_api(api) -> None:
    """Attach the article-video routes to the shared Wagtail v3 ``NinjaAPI``.

    Must be called before ``api.urls`` is first accessed (i.e. before the URLconf
    is built). Registering the same router twice is guarded against so repeated
    imports / test reloads do not raise.
    """
    already = any(router is video_router for _, router in api._routers)
    if not already:
        api.add_router("/pages/", video_router)
