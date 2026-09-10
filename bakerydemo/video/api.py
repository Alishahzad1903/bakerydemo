"""HTTP API for producing an article video, mounted on the Wagtail v3 API.

Two routes are added under the existing ``/api/v3-preview/pages/`` tree, using the
v3 API's own conventions (Django-Ninja router, ``BearerTokenAuth`` against
``wagtailcore.APIToken``, and the ``require_any_permission`` publish gate):

* ``POST /api/v3-preview/pages/{page_id}/video/`` -> ``{ "videoJobId": ... }`` (202)
* ``GET  /api/v3-preview/pages/{page_id}/video/{video_job_id}/``
      -> ``{ "status", "progressPercentage", "downloadUrl", "error", "videoJobId" }``

Producing a video is an editorial action, so both routes require the caller to be
permitted to *publish the page* (``page.permissions_for_user(user).can_publish()``),
mirroring the built-in publish action.
"""

from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import services
from .models import ArticleVideo

# Auth is enforced on every route in this router, exactly like the built-in
# page-action router.
video_router = Router(auth=BearerTokenAuth(), tags=["pages"])


class StartVideoResponse(Schema):
    videoJobId: str


class VideoStatusResponse(Schema):
    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


def _get_publishable_article(request, page_id: int) -> BlogPage:
    """Resolve the page, ensure it's a blog article the caller may publish."""
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        raise HttpError(400, "Videos can only be produced for blog articles.")
    if not page.permissions_for_user(request.user).can_publish():
        # PermissionDenied -> 403 (or 401 if the token didn't authenticate a user),
        # handled by the v3 API's exception handlers.
        raise PermissionDenied("You do not have permission to publish this page.")
    return page


@video_router.post(
    "/{page_id}/video/",
    response={202: StartVideoResponse},
    url_name="pages_video_start",
    summary="Produce a video for a blog article",
    operation_id="pages_video_start",
)
@require_any_permission(Page, ("publish",))
def start_video(request, page_id: int):
    page = _get_publishable_article(request, page_id)
    if not page.live:
        raise HttpError(
            409,
            "This article is not published; publish it before producing a video.",
        )
    if not getattr(settings, "VIDEOGEN_API_KEY", None):
        raise HttpError(503, "The VideoGen integration is not configured.")

    job, _created = services.start_video_for_page(page)
    return 202, {"videoJobId": str(job.id)}


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoStatusResponse,
    url_name="pages_video_status",
    summary="Get the state and outcome of an article video job",
    operation_id="pages_video_status",
)
@require_any_permission(Page, ("publish",))
def get_video_status(request, page_id: int, video_job_id: UUID):
    page = _get_publishable_article(request, page_id)
    job = get_object_or_404(ArticleVideo, pk=video_job_id, page_id=page.pk)

    # When the video is ready, make sure the signed download URL is fresh so it
    # stays usable for as long as the article exists.
    services.refresh_download_url_if_stale(job)

    return {
        "videoJobId": str(job.id),
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": job.download_url or None,
        "error": job.error or None,
    }


_registered = False


def register_video_routes() -> None:
    """Attach the video router to the shared v3 ``api`` singleton.

    Must run before ``api.urls`` is accessed (Django-Ninja finalises the router
    set on first access). Called from ``VideoConfig.ready()`` at startup.
    """
    global _registered
    if _registered:
        return
    # Importing here (not at module load) keeps app loading order flexible and
    # ensures the v3 API singleton is fully built before we extend it.
    from wagtail.api.v3.urls import api

    api.add_router("/pages/", video_router)
    _registered = True
