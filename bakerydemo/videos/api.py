"""VideoGen endpoints on the existing Wagtail v3 API.

Two routes are added under the v3 API's ``/pages/`` prefix, following that
API's own conventions (Django Ninja, ``BearerTokenAuth``, RFC 7807 error
responses, page-permission gating):

    POST /api/v3-preview/pages/{page_id}/video/
        Start producing a video for a published blog article. Returns
        ``{"videoJobId": ...}``. Does not wait for production to finish.

    GET  /api/v3-preview/pages/{page_id}/video/{video_job_id}/
        Report a job's state: ``status`` (processing | ready | failed),
        ``progressPercentage``, ``downloadUrl`` (when ready), ``error`` (when
        failed).

Producing a video is an editorial action, so both routes require a caller
permitted to publish the page.
"""

from __future__ import annotations

import logging
import uuid

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Schema
from ninja.errors import HttpError
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .exceptions import VideoGenError
from .models import ArticleVideo
from .narration import build_narration_script
from .producer import start_production
from .service import VideoGenService

logger = logging.getLogger(__name__)

router = Router(tags=["video"])


# The response field names are the external API contract (camelCase), so they
# intentionally do not follow Python's snake_case convention.
class StartVideoResponse(Schema):
    videoJobId: str


class VideoStatusResponse(Schema):
    status: str
    progressPercentage: float
    downloadUrl: str | None = None
    error: str | None = None


def _get_blog_article(page_id: int) -> BlogPage:
    """Resolve ``page_id`` to a published BlogPage, or raise the right error."""
    page = get_object_or_404(Page, pk=page_id)
    specific = page.specific
    if not isinstance(specific, BlogPage):
        raise Http404("No blog article matches the given page id.")
    return specific


def _require_can_publish(request: HttpRequest, page: Page) -> None:
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied("You do not have permission to publish this page.")


def _get_or_start_video(page: BlogPage) -> ArticleVideo:
    """Return the page's in-flight/ready video job, starting one if needed.

    Idempotent for the "ask twice in a row" case: an existing job that has not
    failed is returned as-is, so no second video is produced or billed. A
    previously failed job does not block a fresh attempt.
    """
    existing = (
        ArticleVideo.objects.filter(page_id=page.pk)
        .exclude(status=ArticleVideo.Status.FAILED)
        .order_by("-created_at")
        .first()
    )
    if existing is not None:
        return existing

    script = build_narration_script(page.title, page.introduction or "")
    job = ArticleVideo.objects.create(page=page, script=script)
    start_production(job)
    return job


def _fresh_download_url(job: ArticleVideo) -> str | None:
    """Return a currently-valid signed MP4 URL for a ready job.

    Re-reads the export from VideoGen so the URL is re-signed and stays usable
    for as long as the article exists. Falls back to the last known URL if the
    provider is momentarily unreachable.
    """
    if job.status != ArticleVideo.Status.READY:
        return None
    if not (job.project_id and job.export_id):
        return job.download_url or None
    try:
        snapshot = VideoGenService().get_project_export(job.project_id, job.export_id)
    except VideoGenError as exc:
        logger.warning("videogen: could not refresh download URL for %s: %s", job.job_id, exc)
        return job.download_url or None

    url = snapshot.download_url or job.download_url or None
    if url and url != job.download_url:
        job.download_url = url
        job.save(update_fields=["download_url", "updated_at"])
    return url


@router.post(
    "/{page_id}/video/",
    response={202: StartVideoResponse},
    auth=BearerTokenAuth(),
    url_name="pages_video_start",
    operation_id="pages_video_start",
    summary="Start producing a video for a blog article",
)
@require_any_permission(Page, ("publish",))
def start_article_video(request: HttpRequest, page_id: int):
    page = _get_blog_article(page_id)
    _require_can_publish(request, page)
    if not page.live:
        raise HttpError(409, "The blog article is not published.")

    job = _get_or_start_video(page)
    return 202, {"videoJobId": str(job.job_id)}


@router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoStatusResponse,
    auth=BearerTokenAuth(),
    url_name="pages_video_status",
    operation_id="pages_video_status",
    summary="Get the state of an article video job",
)
@require_any_permission(Page, ("publish",))
def get_article_video(request: HttpRequest, page_id: int, video_job_id: str):
    page = get_object_or_404(Page, pk=page_id)
    _require_can_publish(request, page)

    try:
        job_uuid = uuid.UUID(video_job_id)
    except ValueError as exc:
        raise Http404("No video job matches the given id.") from exc

    job = get_object_or_404(ArticleVideo, page_id=page_id, job_id=job_uuid)

    return {
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": _fresh_download_url(job),
        "error": job.error or None,
    }


_registered = False


def register_routes() -> None:
    """Register the video routes on the shared Wagtail v3 API instance.

    Called from the app's ``ready()`` so it runs before the URLconf accesses
    ``api.urls`` (Django Ninja forbids adding routers afterwards).
    """
    global _registered
    if _registered:
        return
    from wagtail.api.v3.urls import api

    api.add_router("/pages/", router)
    _registered = True
