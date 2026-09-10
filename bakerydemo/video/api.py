"""HTTP API for producing and retrieving an article's video.

Mounted onto the existing Wagtail v3 API (``/api/v3-preview/``) so it shares that
API's auth (``BearerTokenAuth``) and conventions. Producing a video is an
editorial action on a page, so it is restricted to callers permitted to
**publish** that page — both at the model level (``require_any_permission``) and
per-page (``page.permissions_for_user(...).can_publish()``), mirroring the v3
``publish`` action.

Endpoints (under ``/api/v3-preview/``):

* ``POST   /pages/{page_id}/video/``                  — start producing a video.
* ``GET    /pages/{page_id}/video/{job_id}/``         — state and outcome.
* ``GET    /pages/{page_id}/video/{job_id}/download/`` — stream the finished MP4.
"""

import uuid

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Schema
from ninja.responses import Status
from pydantic import PositiveInt
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from .models import ArticleVideo, VideoStatus
from .narration import build_narration_for_page
from .worker import run_in_background

# Authenticated exactly like the existing v3 write/action endpoints.
router = Router(auth=BearerTokenAuth())

_registered = False


def register() -> None:
    """Register this router on the v3 API singleton.

    Must run before ``api.urls`` is accessed (Django Ninja freezes routers then),
    so it is called from ``VideoConfig.ready()``. Guarded against double
    registration.
    """
    global _registered
    if _registered:
        return
    from wagtail.api.v3.api import api

    api.add_router("/pages/", router)
    _registered = True


class VideoJobSchema(Schema):
    """Wire shape for a video job. Field names are the exact top-level keys."""

    videoJobId: str
    status: str
    progressPercentage: int
    downloadUrl: str | None = None
    error: str | None = None


def _get_publishable_blog_page(request: HttpRequest, page_id: int) -> BlogPage:
    """Resolve a live blog article the caller is allowed to publish.

    404 if there is no such published blog article; 403 if the caller cannot
    publish it.
    """
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage) or not page.live:
        raise Http404("No published blog article matches the given id.")
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied
    return page


def _serialize(request: HttpRequest, page_id: int, job: ArticleVideo) -> dict:
    download_url = None
    if job.is_ready:
        download_url = request.build_absolute_uri(
            reverse(
                "wagtailapi_v3:video_download",
                kwargs={"page_id": page_id, "job_id": str(job.job_id)},
            )
        )
    return {
        "videoJobId": str(job.job_id),
        "status": job.status,
        "progressPercentage": job.progress_percentage,
        "downloadUrl": download_url,
        "error": job.error or None,
    }


@router.post(
    "/{page_id}/video/",
    response={200: VideoJobSchema, 202: VideoJobSchema},
    url_name="video_create",
    summary="Produce a video for a blog article",
    operation_id="pages_video_create",
)
@require_any_permission(Page, ("publish",))
def create_article_video(request: HttpRequest, page_id: PositiveInt):
    """Start (or return the existing) video production for one article.

    Idempotent per article: the ``OneToOne`` relation guarantees a single job, so
    asking twice never starts a second workflow or export. Returns 202 when a new
    job is started, 200 when an existing one is returned.
    """
    page = _get_publishable_blog_page(request, page_id)

    job, created = ArticleVideo.objects.get_or_create(
        page=page,
        defaults={
            "script": build_narration_for_page(page),
            "status": VideoStatus.PENDING,
        },
    )

    if created:
        # Start the pipeline only once the row is safely committed.
        transaction.on_commit(lambda: run_in_background(job))
        return Status(202, _serialize(request, page_id, job))
    return Status(200, _serialize(request, page_id, job))


@router.get(
    "/{page_id}/video/{job_id}/",
    response=VideoJobSchema,
    url_name="video_detail",
    summary="Get the state and outcome of a video job",
    operation_id="pages_video_detail",
)
@require_any_permission(Page, ("publish",))
def get_article_video(request: HttpRequest, page_id: PositiveInt, job_id: uuid.UUID):
    page = _get_publishable_blog_page(request, page_id)
    job = get_object_or_404(ArticleVideo, job_id=job_id, page=page)
    return _serialize(request, page_id, job)


@router.get(
    "/{page_id}/video/{job_id}/download/",
    response={200: None},
    url_name="video_download",
    summary="Download the finished MP4",
    operation_id="pages_video_download",
)
@require_any_permission(Page, ("publish",))
def download_article_video(
    request: HttpRequest, page_id: PositiveInt, job_id: uuid.UUID
):
    page = _get_publishable_blog_page(request, page_id)
    job = get_object_or_404(ArticleVideo, job_id=job_id, page=page)
    if not job.is_ready:
        raise Http404("The video for this article is not ready to download.")
    return FileResponse(
        job.video_file.open("rb"),
        as_attachment=True,
        filename=f"{page.slug}.mp4",
        content_type="video/mp4",
    )
