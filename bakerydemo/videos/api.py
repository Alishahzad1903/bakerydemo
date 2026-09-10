"""Video endpoints, mounted onto the existing Wagtail v3-preview API.

Routes (under the API's ``/pages/`` prefix, i.e. ``/api/v3-preview/pages/``):

* ``POST /{page_id}/video/`` — start producing a video for a published article.
  Returns ``202`` with ``{"videoJobId": ...}``. Idempotent per article.
* ``GET  /{page_id}/video/{video_job_id}/`` — status/outcome of one request:
  ``{status, progressPercentage, downloadUrl, error}``.
* ``GET  /{page_id}/video/{video_job_id}/download/`` — stream the finished MP4.

Authentication and permissions follow the v3 API's own conventions exactly:
bearer-token auth via :class:`BearerTokenAuth`, and — because producing a video
is an editorial action on a page — the caller must be permitted to *publish*
that page.
"""

from __future__ import annotations

from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Schema
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .models import VideoJob
from .narration import build_narration_script
from .production import ensure_production_configured, start_production

video_router = Router(tags=["pages"], auth=BearerTokenAuth())


# -- response schemas ----------------------------------------------------


class VideoJobStartSchema(Schema):
    """202 response for a start-video request."""

    videoJobId: str


class VideoJobStatusSchema(Schema):
    """State and outcome of one video request.

    ``status`` lets a caller tell, without guessing, whether the video is still
    being produced (``pending``/``running``), ready (``succeeded``), or failed
    (``failed``). ``downloadUrl`` is populated only when ready; ``error`` only
    when failed.
    """

    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


# -- helpers -------------------------------------------------------------


def _resolve_publishable_article(request: HttpRequest, page_id: int) -> BlogPage:
    """Return the ``BlogPage`` the caller may publish, or raise 404/403.

    404 for anything that is not an existing blog article; ``PermissionDenied``
    (rendered as 401/403 by the v3 error handlers) when the caller cannot
    publish it.
    """
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        raise Http404("No blog article matches the given id.")
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")
    return page


def _get_job(page: BlogPage, video_job_id: UUID) -> VideoJob:
    return get_object_or_404(VideoJob, pk=video_job_id, page=page)


def _download_url(request: HttpRequest, job: VideoJob) -> str | None:
    if job.status != VideoJob.Status.SUCCEEDED or not job.video_file:
        return None
    path = reverse(
        "wagtailapi_v3:pages_video_download",
        kwargs={"page_id": job.page_id, "video_job_id": str(job.id)},
    )
    return request.build_absolute_uri(path)


def _serialize(request: HttpRequest, job: VideoJob) -> dict:
    return {
        "videoJobId": str(job.id),
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": _download_url(request, job),
        "error": job.error or None,
    }


# -- endpoints -----------------------------------------------------------


@video_router.post(
    "/{page_id}/video/",
    response={202: VideoJobStartSchema},
    url_name="pages_video_create",
    summary="Produce a video for an article",
    operation_id="pages_video_create",
)
def start_video(request: HttpRequest, page_id: int):
    page = _resolve_publishable_article(request, page_id)
    if not page.live:
        # Only *published* articles can be turned into a video.
        raise HttpError(409, "This article is not published.")

    # Validate provider capability before creating a job, so a request that
    # cannot be fulfilled never creates a (blocking) job and the reason is
    # surfaced clearly. Raises a typed VideoGenError mapped to a problem+json.
    ensure_production_configured()

    script = build_narration_script(page)

    # Idempotent per article: an already-active or already-finished-successfully
    # production is reused, so asking twice never produces (or bills for) two
    # videos. Only a previously *failed* job allows a fresh attempt.
    with transaction.atomic():
        existing = (
            VideoJob.objects.select_for_update()
            .filter(page=page, status__in=VideoJob.ACTIVE)
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            job, created = existing, False
        else:
            job = VideoJob.objects.create(
                page=page,
                script=script,
                status=VideoJob.Status.PENDING,
            )
            created = True

    if created:
        start_production(job.id)

    return 202, {"videoJobId": str(job.id)}


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    url_name="pages_video_status",
    summary="Video request status",
    operation_id="pages_video_status",
)
def video_status(request: HttpRequest, page_id: int, video_job_id: UUID):
    page = _resolve_publishable_article(request, page_id)
    job = _get_job(page, video_job_id)
    return _serialize(request, job)


@video_router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="pages_video_download",
    summary="Download the finished MP4",
    operation_id="pages_video_download",
)
def video_download(request: HttpRequest, page_id: int, video_job_id: UUID):
    page = _resolve_publishable_article(request, page_id)
    job = _get_job(page, video_job_id)
    if job.status != VideoJob.Status.SUCCEEDED or not job.video_file:
        raise Http404("No finished video is available for this request.")
    response = FileResponse(
        job.video_file.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"{page.slug or 'article'}-{job.id}.mp4",
    )
    return response


# -- registration onto the shared v3 API ---------------------------------

_REGISTERED = False


def register() -> None:
    """Attach the video router and VideoGen error handlers to the v3 API.

    Called from ``VideosConfig.ready()`` so it runs during ``django.setup()``,
    before the URLconf accesses ``api.urls`` (Django Ninja requires routers to
    be registered before then).
    """
    global _REGISTERED
    if _REGISTERED:
        return

    from wagtail.api.v3.api import api
    from wagtail.api.v3.errors import problem_response

    from .videogen import (
        VideoGenCapabilityUnavailable,
        VideoGenConfigurationError,
        VideoGenError,
    )

    api.add_router("/pages/", video_router)

    @api.exception_handler(VideoGenCapabilityUnavailable)
    def _capability_unavailable(request, exc):
        # A mandated capability is not covered/configured. The endpoint is
        # implemented; the upstream capability is unavailable → 503.
        return problem_response(
            status=503,
            title="Video production unavailable",
            detail=str(exc),
        )

    @api.exception_handler(VideoGenConfigurationError)
    def _configuration_error(request, exc):
        return problem_response(
            status=503,
            title="Video production not configured",
            detail=str(exc),
        )

    @api.exception_handler(VideoGenError)
    def _provider_error(request, exc):
        return problem_response(
            status=502,
            title="Video provider error",
            detail=str(exc),
        )

    _REGISTERED = True
