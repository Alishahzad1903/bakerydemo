"""VideoGen endpoints for the Wagtail v3 API.

These are mounted onto the *existing* ``/api/v3-preview/`` API and follow its
conventions exactly: a Django Ninja ``Router`` authenticated with the same
``BearerTokenAuth`` the rest of the write API uses, ``PermissionDenied`` for
authorization failures (rendered as RFC 7807 ``problem+json`` by the API's
shared exception handlers), and camelCase response fields.

Endpoints (relative to ``/api/v3-preview/``):

* ``POST /pages/{page_id}/video/``                       -> start a video
* ``GET  /pages/{page_id}/video/{video_job_id}/``        -> job state/outcome
* ``GET  /pages/{page_id}/video/{video_job_id}/download/`` -> the finished MP4

Producing a video is an editorial action, so every endpoint is restricted to
callers permitted to publish the target page.
"""

from __future__ import annotations

import uuid

import swapper
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Schema, Status
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth

from .models import VideoJob
from .service import start_video_job

Page = swapper.load_model("wagtailcore", "Page")

video_router = Router(auth=BearerTokenAuth(), tags=["pages"])


# -- response schemas ------------------------------------------------------


class VideoJobStartSchema(Schema):
    videoJobId: str
    status: str


class VideoJobStatusSchema(Schema):
    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


# -- helpers ---------------------------------------------------------------


def _get_publishable_page(request: HttpRequest, page_id: int) -> Page:
    """Return the page, enforcing that the caller may publish it.

    Raises ``Http404`` if the page does not exist and ``PermissionDenied`` (403)
    if the authenticated caller lacks publish permission for that page.
    """
    page = get_object_or_404(Page, pk=page_id).specific
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")
    return page


def _get_job(page: Page, video_job_id: uuid.UUID) -> VideoJob:
    try:
        return VideoJob.objects.get(pk=video_job_id, page=page)
    except VideoJob.DoesNotExist as exc:
        raise Http404("No video job matches the given id for this page.") from exc


def _download_url(request: HttpRequest, job: VideoJob) -> str | None:
    if not job.is_ready or not job.video_file:
        return None
    path = reverse(
        "wagtailapi_v3:pages_video_download",
        kwargs={"page_id": job.page_id, "video_job_id": str(job.id)},
    )
    return request.build_absolute_uri(path)


def _status_payload(request: HttpRequest, job: VideoJob) -> dict:
    return {
        "videoJobId": str(job.id),
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": _download_url(request, job),
        "error": job.error or None,
    }


# -- endpoints -------------------------------------------------------------


@video_router.post(
    "/{page_id}/video/",
    response={202: VideoJobStartSchema, 200: VideoJobStartSchema},
    url_name="pages_video_create",
    summary="Produce a video for a page",
    operation_id="pages_video_create",
)
def create_video(request: HttpRequest, page_id: int):
    """Start producing a short narrated video for a published article.

    Returns immediately with ``202 Accepted`` while the video is produced in the
    background. Asking again for the same article returns the existing job with
    ``200 OK`` — it does not start (or bill for) a second video.
    """
    page = _get_publishable_page(request, page_id)
    if not page.live:
        raise HttpError(409, "Cannot produce a video for a page that is not published.")

    job, started = start_video_job(page)
    data = {"videoJobId": str(job.id), "status": job.status}
    return Status(202 if started else 200, data)


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    url_name="pages_video_detail",
    summary="Get the state of a video job",
    operation_id="pages_video_detail",
)
def get_video(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    """Report whether the video is still being produced, ready, or failed.

    ``status`` is one of ``pending``, ``processing``, ``ready`` or ``failed``.
    ``downloadUrl`` is populated only when ``status`` is ``ready``; ``error`` is
    populated only when ``status`` is ``failed``.
    """
    page = _get_publishable_page(request, page_id)
    job = _get_job(page, video_job_id)
    return _status_payload(request, job)


@video_router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="pages_video_download",
    summary="Download the finished MP4",
    operation_id="pages_video_download",
)
def download_video(request: HttpRequest, page_id: int, video_job_id: uuid.UUID):
    """Stream the finished MP4, stored on the site for as long as the article
    exists."""
    page = _get_publishable_page(request, page_id)
    job = _get_job(page, video_job_id)
    if not job.is_ready or not job.video_file:
        raise Http404("The video for this job is not ready to download.")
    response = FileResponse(
        job.video_file.open("rb"),
        content_type="video/mp4",
        as_attachment=True,
        filename=f"page-{page.id}-video.mp4",
    )
    return response


def register_video_api(api) -> None:
    """Mount the video endpoints onto the given Wagtail v3 ``NinjaAPI``.

    Called from the project URLconf *before* ``api.urls`` is accessed, alongside
    the built-in ``pages`` router (both live under the ``/pages/`` prefix).
    """
    api.add_router("/pages/", video_router)
