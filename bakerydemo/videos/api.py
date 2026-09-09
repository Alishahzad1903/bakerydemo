"""Django-Ninja endpoints for producing a video from a blog article.

Mounted onto the shared Wagtail v3 (``v3-preview``) API under ``/pages/``, so
the routes are:

- ``POST /api/v3-preview/pages/{page_id}/video/`` — start producing a video.
- ``GET  /api/v3-preview/pages/{page_id}/video/{video_job_id}/`` — its state.
- ``GET  /api/v3-preview/pages/{page_id}/video/{video_job_id}/download/`` —
  the finished MP4 (this is where ``downloadUrl`` points).

Callers authenticate exactly as the rest of the v3 API does (``BearerTokenAuth``
against ``APIToken``). Producing a video is an editorial action, so it is
restricted to callers permitted to **publish** the target page — the same gate
the built-in publish action uses.
"""

import uuid

from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router
from ninja.errors import HttpError
from pydantic import PositiveInt
from wagtail.api.v3.auth import BearerTokenAuth
from wagtail.api.v3.permissions import require_any_permission
from wagtail.models import Page

from bakerydemo.blog.models import BlogPage

from . import service
from .models import ArticleVideo
from .schemas import StartVideoResponse, VideoStatusResponse

video_router = Router(auth=BearerTokenAuth(), tags=["videos"])


def _get_publishable_article(request, page_id: int) -> BlogPage:
    """Resolve a published blog article the caller may publish, or raise.

    - unknown page / not a blog article -> 404
    - the article is not published (draft) -> 409
    - the caller cannot publish this page -> 403 (401 if unauthenticated, but
      ``BearerTokenAuth`` already rejects anonymous callers upstream)
    """
    page = get_object_or_404(Page, pk=page_id).specific
    if not isinstance(page, BlogPage):
        raise Http404("No blog article matches the given page id.")
    if not page.live:
        raise HttpError(409, "This blog article is not published.")
    if not page.permissions_for_user(request.user).can_publish():
        raise PermissionDenied
    return page


def _download_url(request, page_id: int, job_id) -> str:
    return request.build_absolute_uri(
        reverse(
            "wagtailapi_v3:videos_download",
            kwargs={"page_id": page_id, "video_job_id": str(job_id)},
        )
    )


def _status_payload(request, page_id: int, video: ArticleVideo) -> dict:
    return {
        "videoJobId": str(video.job_id),
        "status": video.status,
        "progressPercentage": video.progress,
        "downloadUrl": (
            _download_url(request, page_id, video.job_id) if video.is_ready else None
        ),
        "error": video.error or None,
    }


@video_router.post(
    "/{page_id}/video/",
    response={200: StartVideoResponse, 202: StartVideoResponse},
    url_name="videos_start",
    operation_id="pages_video_start",
    summary="Produce a video for a blog article",
)
@require_any_permission(Page, ("publish",))
def start_video(request, page_id: PositiveInt):
    page = _get_publishable_article(request, page_id)
    video, started = service.start_video_for_page(page)
    status_code = 202 if started else 200
    return status_code, {"videoJobId": str(video.job_id)}


@video_router.get(
    "/{page_id}/video/{video_job_id}/",
    response=VideoStatusResponse,
    url_name="videos_status",
    operation_id="pages_video_status",
    summary="State of a blog article's video",
)
@require_any_permission(Page, ("publish",))
def video_status(request, page_id: PositiveInt, video_job_id: uuid.UUID):
    page = _get_publishable_article(request, page_id)
    video = get_object_or_404(ArticleVideo, job_id=video_job_id, page_id=page.pk)
    return _status_payload(request, page_id, video)


@video_router.get(
    "/{page_id}/video/{video_job_id}/download/",
    url_name="videos_download",
    operation_id="pages_video_download",
    summary="Download a blog article's finished video",
)
@require_any_permission(Page, ("publish",))
def video_download(request, page_id: PositiveInt, video_job_id: uuid.UUID):
    page = _get_publishable_article(request, page_id)
    video = get_object_or_404(ArticleVideo, job_id=video_job_id, page_id=page.pk)
    if not video.is_ready:
        raise Http404("The video is not ready to download.")
    return FileResponse(
        video.mp4.open("rb"),
        as_attachment=True,
        filename=f"{page.slug}.mp4",
        content_type="video/mp4",
    )


# Register onto the shared v3 ``api`` instance. This module is imported from
# ``VideosConfig.ready()`` — before ``api.urls`` is evaluated in
# ``bakerydemo.urls`` — which is when Ninja allows routers to be added.
from wagtail.api.v3.urls import api  # noqa: E402

api.add_router("/pages/", video_router)
