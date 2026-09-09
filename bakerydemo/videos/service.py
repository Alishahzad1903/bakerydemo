"""Orchestration of a single article-to-video production.

The full pipeline, driven synchronously from a background thread:

1. ``script_to_video`` — start a workflow run from the verbatim narration,
   with STOCK visuals (stock footage, no AI-generated imagery) and no actor
   (voiceover only, no on-screen presenter/avatar).
2. Poll ``get_workflow_run`` until the run is terminal.
3. ``export_project`` — render the built project to an MP4 at the configured
   resolution tier (1080p or below).
4. Poll ``get_project_export`` until the export is terminal.
5. Download the signed MP4 and store it on the ``ArticleVideo`` row.

Every VideoGen SDK call goes through :func:`_translate`, which converts the
SDK's failure kinds into the integration's typed exceptions. Request bodies are
passed as the SDK's ``…Dict`` companions (keyed by the Python member names), the
type-checked way to build them.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

import httpx
from django.conf import settings
from django.core.files import File
from pydantic import ValidationError
from videogen import VideogenClient
from videogen.core import ApiError, RawError
from videogen.models import ExportProjectRequestDict, ScriptToVideoRequestDict
from videogen.models.enums import ExportProjectQuality, WorkflowVisualStyleType

from .client import get_client
from .exceptions import (
    VideoGenApiError,
    VideoGenConfigurationError,
    VideoGenContentError,
    VideoGenProductionError,
    VideoGenUnreachableError,
    VideoGenUnreadableResponseError,
)
from .narration import build_narration

if TYPE_CHECKING:
    from .models import ArticleVideo

# In-progress and terminal job statuses, matched on the wire string so an
# unknown future JobStatus member is handled rather than crashing.
_SUCCEEDED = "succeeded"
_TERMINAL_FAILURES = {"failed", "cancelled"}

# Progress bands (percent) for each phase of the composite progress bar.
_WORKFLOW_BAND = (1.0, 85.0)
_EXPORT_BAND = (85.0, 98.0)
_DOWNLOAD_MARK = 98.0

# A downloader is injectable so tests need not reach the network.
Downloader = Callable[[str], bytes]


class _PollableJob(Protocol):
    """Common shape of a WorkflowRun / ProjectExport while polling."""

    @property
    def status(self) -> object: ...

    @property
    def progress_percentage(self) -> float: ...

    @property
    def error(self) -> object: ...


def _api_error_message(error: ApiError) -> str:
    """Human-readable text for a Case-B ``RawError`` payload."""
    payload = error.error
    if isinstance(payload, RawError):
        try:
            text = payload.text()
        except UnicodeDecodeError:  # pragma: no cover - non-decodable body
            text = ""
        return text or f"HTTP {error.status_code}"
    return f"HTTP {error.status_code}"


def _translate[T](call: Callable[[], T]) -> T:
    """Run one SDK call, translating every failure kind into a typed error.

    Ordering matters: ``ValidationError`` is a ``ValueError`` subclass, and a
    non-JSON decode failure raises a plain ``ValueError`` — both are unreadable
    responses. ``httpx`` transport errors arrive unwrapped.
    """
    try:
        return call()
    except ApiError as exc:
        raise VideoGenApiError(exc.status_code, _api_error_message(exc)) from exc
    except ValidationError as exc:
        raise VideoGenUnreadableResponseError(
            "VideoGen returned an unreadable response; outcome unknown."
        ) from exc
    except httpx.HTTPError as exc:
        raise VideoGenUnreachableError(
            "VideoGen is unreachable; outcome unknown."
        ) from exc
    except ValueError as exc:
        raise VideoGenUnreadableResponseError(
            "VideoGen returned an unreadable response; outcome unknown."
        ) from exc


def _resolve_quality() -> ExportProjectQuality:
    """Validate the configured export quality up front, before any billing."""
    configured = settings.VIDEOGEN_EXPORT_QUALITY
    try:
        return ExportProjectQuality(configured)
    except ValueError as exc:
        raise VideoGenConfigurationError(
            f"VIDEOGEN_EXPORT_QUALITY={configured!r} is not a valid tier; "
            f"expected one of {[q.value for q in ExportProjectQuality]}."
        ) from exc


def _job_error_message(job: _PollableJob) -> str | None:
    error = job.error
    message = getattr(error, "message", None)
    return message if isinstance(message, str) and message else None


def _download_bytes(url: str) -> bytes:
    """Fetch the signed MP4. Not an SDK call — a plain HTTP GET."""
    try:
        with httpx.stream(
            "GET", url, timeout=settings.VIDEOGEN_TIMEOUT * 4, follow_redirects=True
        ) as response:
            response.raise_for_status()
            return response.read()
    except httpx.HTTPError as exc:
        raise VideoGenUnreachableError(
            "Failed to download the finished MP4 from VideoGen."
        ) from exc


def _poll[JobT: _PollableJob](
    fetch: Callable[[], JobT],
    *,
    article_video: ArticleVideo,
    band: tuple[float, float],
    phase: str,
) -> JobT:
    """Poll ``fetch`` until the returned job is terminal, updating progress.

    Returns the succeeded job, or raises :class:`VideoGenProductionError` on a
    terminal failure/cancellation or on timeout.
    """
    low, high = band
    deadline = time.monotonic() + settings.VIDEOGEN_MAX_WAIT_SECONDS
    interval = settings.VIDEOGEN_POLL_INTERVAL
    while True:
        job = _translate(fetch)
        status = str(job.status)  # open enum -> wire string
        percentage = max(0.0, min(100.0, float(job.progress_percentage or 0.0)))
        article_video.set_progress(low + (high - low) * percentage / 100.0)

        if status == _SUCCEEDED:
            return job
        if status in _TERMINAL_FAILURES:
            message = _job_error_message(job) or f"VideoGen {phase} {status}."
            raise VideoGenProductionError(message)
        if time.monotonic() >= deadline:
            raise VideoGenProductionError(
                f"VideoGen {phase} did not finish within "
                f"{settings.VIDEOGEN_MAX_WAIT_SECONDS:.0f}s."
            )
        time.sleep(interval)


def produce_video(
    article_video: ArticleVideo,
    *,
    client: VideogenClient | None = None,
    downloader: Downloader | None = None,
) -> None:
    """Run the whole pipeline for ``article_video``, updating it as it goes.

    On success the row ends ``READY`` with a stored MP4. On any provider
    failure a typed :class:`VideoGenError` propagates (the caller records it).
    """
    client = client or get_client()
    download = downloader or _download_bytes
    page = article_video.page.specific

    script = build_narration(page)
    if not script:
        raise VideoGenContentError(
            "The article has no narratable text (title, introduction or body)."
        )
    quality = _resolve_quality()

    article_video.mark_processing(percentage=_WORKFLOW_BAND[0])

    # 1. Start the workflow run. STOCK visuals + no actor => stock footage,
    #    voiceover only, no on-screen presenter.
    script_body: ScriptToVideoRequestDict = {
        "script": script,
        "visual_style": {"type_": WorkflowVisualStyleType.STOCK},
        "aspect_ratio": {"width": 16, "height": 9},
    }
    start = _translate(lambda: client.workflows.script_to_video(body=script_body))
    workflow_run_id = str(start.workflow_run_id) if start.workflow_run_id else ""
    project_id = str(start.project_id) if start.project_id else ""
    if not workflow_run_id or not project_id:
        raise VideoGenUnreadableResponseError(
            "VideoGen did not return a workflow run id; outcome unknown."
        )
    article_video.videogen_workflow_run_id = workflow_run_id
    article_video.videogen_project_id = project_id
    article_video.save(
        update_fields=[
            "videogen_workflow_run_id",
            "videogen_project_id",
            "updated_at",
        ]
    )

    # 2. Wait for the workflow to build the project.
    _poll(
        lambda: client.workflows.get_workflow_run(workflow_run_id),
        article_video=article_video,
        band=_WORKFLOW_BAND,
        phase="workflow",
    )

    # 3. Export the built project to an MP4 at <=1080p.
    article_video.set_progress(_EXPORT_BAND[0])
    export_body: ExportProjectRequestDict = {"quality": quality}
    export = _translate(
        lambda: client.projects.export_project(project_id, body=export_body)
    )
    export_id = str(export.export_id) if export.export_id else ""
    if not export_id:
        raise VideoGenUnreadableResponseError(
            "VideoGen did not return an export id; outcome unknown."
        )
    article_video.videogen_export_id = export_id
    article_video.save(update_fields=["videogen_export_id", "updated_at"])

    # 4. Wait for the export to finish and yield a signed download URL.
    project_export = _poll(
        lambda: client.projects.get_project_export(project_id, export_id),
        article_video=article_video,
        band=_EXPORT_BAND,
        phase="export",
    )
    download_url = project_export.download_url
    if not download_url:
        raise VideoGenUnreadableResponseError(
            "VideoGen export succeeded without a download URL; outcome unknown."
        )

    # 5. Download the MP4 and re-host it on the site.
    article_video.set_progress(_DOWNLOAD_MARK)
    content = download(download_url)
    _store_mp4(article_video, content)
    article_video.mark_ready()


def _store_mp4(article_video: ArticleVideo, content: bytes) -> None:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as handle:
            article_video.video_file.save(
                f"{article_video.job_id}.mp4", File(handle), save=True
            )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
