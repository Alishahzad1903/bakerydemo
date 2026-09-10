"""v3 API endpoints for producing a video from a blog article.

These routes extend the existing Wagtail v3 API (mounted at
``/api/v3-preview/``) and follow its conventions exactly: Django Ninja router,
``BearerTokenAuth`` for authentication, and the same permission gating helpers.
Producing a video is an editorial action, so both endpoints are restricted to
callers permitted to publish the target page.

Endpoints (registered under ``/pages/`` in :mod:`bakerydemo.videogen.apps`):

* ``POST /pages/{page_id}/video/`` — start producing a video; returns
  ``videoJobId``. Idempotent: asking twice returns the same job.
* ``GET  /pages/{page_id}/video/{video_job_id}/`` — the state and outcome:
  ``status``, ``progressPercentage``, ``downloadUrl``, ``error``.
"""

from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.errors import as_validation_error
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import services
from .models import VideoJob

router = Router(tags=["pages"])


# ---------------------------------------------------------------------------
# Response schemas — top-level fields named exactly as the API contract requires.
# ---------------------------------------------------------------------------


class VideoJobCreatedSchema(Schema):
    videoJobId: str
    status: str


class VideoJobStatusSchema(Schema):
    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_published_blog_article(page_id: int) -> BlogPage:
    """Return the published :class:`BlogPage` for ``page_id`` or raise.

    * missing page or non-blog page → 404
    * a blog article that is not published → 422
    """
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        raise Http404("No published blog article matches the given page id.")
    if not page.live:
        message = "Video generation is only available for published blog articles."
        raise as_validation_error(ValueError(message), message, loc=("page_id",))
    return page


def _require_publish_permission(request: HttpRequest, page: Page) -> None:
    """Enforce that the caller may publish ``page`` (an editorial action)."""
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{page_id}/video/",
    response={202: VideoJobCreatedSchema},
    auth=BearerTokenAuth(),
    url_name="pages_video_create",
    summary="Produce a video for a blog article",
    operation_id="pages_video_create",
)
@require_any_permission(Page, ("publish",))
def create_page_video(request: HttpRequest, page_id: int):
    page = _resolve_published_blog_article(page_id)
    _require_publish_permission(request, page)
    job, _created = services.get_or_create_video_job(page)
    return 202, {"videoJobId": str(job.uuid), "status": job.status}


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoJobStatusSchema,
    auth=BearerTokenAuth(),
    url_name="pages_video_detail",
    summary="Get the state and outcome of a video production request",
    operation_id="pages_video_detail",
)
@require_any_permission(Page, ("publish",))
def get_page_video(request: HttpRequest, page_id: int, video_job_id: UUID):
    page = _resolve_published_blog_article(page_id)
    _require_publish_permission(request, page)
    job = get_object_or_404(VideoJob, page=page, uuid=video_job_id)
    return {
        "videoJobId": str(job.uuid),
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": job.download_url(request),
        "error": job.error or None,
    }
