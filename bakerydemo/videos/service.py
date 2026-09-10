"""Business logic: build the narration, talk to VideoGen, and drive the job.

Two layers:

* ``VideoGenGateway`` — the *only* place the VideoGen SDK is called. It builds the
  fixed, cheapest request shape (stock footage, voice-only, 16:9, one 720p export)
  and translates every SDK/transport/decode failure into a typed
  ``VideoGenError`` (``bakerydemo.videos.exceptions``).
* The state machine (``start_video`` / ``advance``) — a persisted,
  advance-on-demand lifecycle. There is no broker or worker in this deployment, so
  each request advances the job one step: ``POST`` starts the build, each ``GET``
  polls the current phase and triggers the single export when the build is done.
  Every provider-triggering transition is a compare-and-set on the row, so
  concurrent requests can never start two builds or two exports (and therefore
  never bill twice).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol, TypeVar

import httpx
from django.conf import settings
from pydantic import ValidationError
from videogen import VideogenClient
from videogen.core import ApiError, RawError
from videogen.models import (
    ApiErrorModel,
    ExportProjectRequestDict,
    ExportProjectResponse,
    ProjectExport,
    ScriptToVideoRequestDict,
    StartWorkflowRunResponse,
    WorkflowRun,
)
from videogen.models.enums import (
    ExportProjectQuality,
    JobStatus,
    JobStatusOrStr,
    WorkflowVisualStyleType,
)

from .exceptions import (
    VideoGenAPIError,
    VideoGenError,
    VideoGenResponseError,
    VideoGenUnavailableError,
)
from .models import VideoJob
from .videogen_client import get_client

T = TypeVar("T")

# --- The fixed, non-negotiable cheap shape (spend limits) --------------------
# These are constants, not settings, so the cheap shape cannot be widened by
# configuration into a billable one (AI imagery, avatars, 4K, image-to-video…).
_ASPECT_WIDTH = 16
_ASPECT_HEIGHT = 9
# 720p vertical tier. The plugin does not document each tier's pixel resolution;
# an export at HIGH was observed to render 1920x1080 (1080p), so the ladder is
# STANDARD=720p < HIGH=1080p < FULL_HIGH < ULTRA_HIGH=4K, and STANDARD is 720p.
# Never ULTRA_HIGH (4K).
_EXPORT_QUALITY = ExportProjectQuality.STANDARD

# Terminal VideoGen job states (JobStatus wire values).
_TERMINAL_OK = frozenset({JobStatus.SUCCEEDED.value})
_TERMINAL_FAIL = frozenset({JobStatus.FAILED.value, JobStatus.CANCELLED.value})

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


# --- Narration ---------------------------------------------------------------


def build_script(title: str, introduction: str, max_words: int) -> str:
    """Narration from the article's own words: its title + the first sentence of
    its introduction, capped at ``max_words``. Nothing else is sent, and the text
    is used verbatim — never rewritten, summarised or expanded elsewhere.
    """
    title = (title or "").strip()
    intro = (introduction or "").strip()

    first_sentence = ""
    if intro:
        first_sentence = _SENTENCE_SPLIT.split(intro, maxsplit=1)[0].strip()

    title_part = title if (title and title[-1] in ".!?") else f"{title}."
    script = f"{title_part} {first_sentence}".strip() if first_sentence else title_part

    words = script.split()
    if len(words) > max_words:
        script = " ".join(words[:max_words])
    return script


# --- Provider gateway --------------------------------------------------------


class SupportsVideoGen(Protocol):
    """Structural interface the state machine depends on (real gateway or a fake)."""

    def start_workflow(self, script: str) -> StartWorkflowRunResponse: ...
    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun: ...
    def start_export(self, project_id: str) -> ExportProjectResponse: ...
    def get_export(self, project_id: str, export_id: str) -> ProjectExport: ...


class VideoGenGateway:
    """The one seam that touches the VideoGen SDK. Every method surfaces failures
    as typed ``VideoGenError`` subclasses and never leaks an SDK/httpx/pydantic
    exception."""

    def __init__(self, client: VideogenClient) -> None:
        self._client = client

    def start_workflow(self, script: str) -> StartWorkflowRunResponse:
        """The single billable production call: script → built project."""
        body: ScriptToVideoRequestDict = {
            "script": script,
            "visual_style": {
                "type_": WorkflowVisualStyleType.STOCK
            },  # stock footage only
            "aspect_ratio": {"width": _ASPECT_WIDTH, "height": _ASPECT_HEIGHT},
            # No actor_entity_id -> voiceover only (no presenter/avatar).
            # No remix_actions, no AI style, is_output_temporary defaults False.
        }
        resp = self._call(lambda: self._client.workflows.script_to_video(body=body))
        if not resp.workflow_run_id or not resp.project_id:
            raise VideoGenResponseError(
                "script_to_video returned no workflow/project id; outcome unknown"
            )
        return resp

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        return self._call(
            lambda: self._client.workflows.get_workflow_run(workflow_run_id)
        )

    def start_export(self, project_id: str) -> ExportProjectResponse:
        """The single 720p export of the built project."""
        body: ExportProjectRequestDict = {"quality": _EXPORT_QUALITY}
        resp = self._call(
            lambda: self._client.projects.export_project(project_id, body=body)
        )
        if not resp.export_id:
            raise VideoGenResponseError(
                "export_project returned no export id; outcome unknown"
            )
        return resp

    def get_export(self, project_id: str, export_id: str) -> ProjectExport:
        return self._call(
            lambda: self._client.projects.get_project_export(project_id, export_id)
        )

    def _call(self, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except ApiError as exc:
            # Every operation in this SDK is Case B: exc.error is RawError.
            detail = (
                exc.error.text() if isinstance(exc.error, RawError) else str(exc.error)
            )
            raise VideoGenAPIError(exc.status_code, detail) from exc
        except (ValidationError, ValueError) as exc:
            # Decode failure (bypasses both SDK response modes). Outcome unknown.
            raise VideoGenResponseError(
                "VideoGen response could not be read; outcome unknown"
            ) from exc
        except httpx.HTTPError as exc:
            raise VideoGenUnavailableError(
                "VideoGen is unreachable; outcome unknown"
            ) from exc


def _gateway() -> VideoGenGateway:
    return VideoGenGateway(get_client())


# --- Helpers -----------------------------------------------------------------


def _status_value(status: JobStatusOrStr) -> str:
    """Wire value for an (open) job status enum-or-str."""
    return status.value if isinstance(status, JobStatus) else str(status)


def _error_text(err: ApiErrorModel | None) -> str:
    if err is None:
        return "Video production failed"
    code = err.code if isinstance(err.code, str) else None
    return f"{err.message} ({code})" if code else err.message


def _build_progress(pct: float) -> float:
    # Build phase occupies 0–90% of the overall bar.
    return min(max(pct, 0.0), 100.0) * 0.9


def _export_progress(pct: float) -> float:
    # Export phase occupies 90–100%.
    return 90.0 + min(max(pct, 0.0), 100.0) * 0.10


def _expires_at(export: ProjectExport) -> int | None:
    value = export.download_url_expires_at
    return value if isinstance(value, int) else None


# --- State machine -----------------------------------------------------------


def start_video(
    page: object, gateway: SupportsVideoGen | None = None
) -> tuple[VideoJob, bool]:
    """Idempotently ensure a job exists for ``page`` and take one step.

    Returns ``(job, created)``. Asking twice for the same article returns the same
    job and never starts a second production (the ``OneToOneField`` guarantees one
    row; the build is only started from the ``PENDING`` state, once).
    """
    title = getattr(page, "title", "") or ""
    introduction = getattr(page, "introduction", "") or ""
    max_words = int(getattr(settings, "VIDEOGEN_MAX_SCRIPT_WORDS", 30))
    script = build_script(title, introduction, max_words)

    job, created = VideoJob.objects.get_or_create(
        page=page, defaults={"script": script}
    )
    advance(job, gateway=gateway)
    job.refresh_from_db()
    return job, created


def advance(job: VideoJob, gateway: SupportsVideoGen | None = None) -> VideoJob:
    """Advance the job by at most one provider interaction, then return it refreshed."""
    gw = gateway or _gateway()
    job.refresh_from_db()

    if job.status == VideoJob.Status.PENDING:
        _start_build(job, gw)
    elif job.status == VideoJob.Status.BUILDING:
        _poll_build(job, gw)
    elif job.status == VideoJob.Status.EXPORTING:
        _poll_export(job, gw)
    elif job.status == VideoJob.Status.READY:
        _refresh_download_url(job, gw)
    # FAILED is terminal: nothing to do.

    job.refresh_from_db()
    return job


def _start_build(job: VideoJob, gw: SupportsVideoGen) -> None:
    # Claim the transition so only one caller ever starts the (billable) build.
    claimed = VideoJob.objects.filter(pk=job.pk, status=VideoJob.Status.PENDING).update(
        status=VideoJob.Status.BUILDING
    )
    if not claimed:
        return

    try:
        resp = gw.start_workflow(job.script)
    except VideoGenError:
        # Roll back so a subsequent request can retry the start. Only rolls back if
        # we are still the un-started claimer (workflow_run_id empty).
        VideoJob.objects.filter(
            pk=job.pk, status=VideoJob.Status.BUILDING, workflow_run_id=""
        ).update(status=VideoJob.Status.PENDING)
        raise

    VideoJob.objects.filter(pk=job.pk).update(
        workflow_run_id=resp.workflow_run_id,
        project_id=resp.project_id,
        progress_percentage=0.0,
    )


def _poll_build(job: VideoJob, gw: SupportsVideoGen) -> None:
    if not job.workflow_run_id:
        return  # claimed but not yet started by a concurrent caller

    run = gw.get_workflow_run(job.workflow_run_id)
    state = _status_value(run.status)

    if state in _TERMINAL_OK:
        _start_export(job, gw)
    elif state in _TERMINAL_FAIL:
        _fail(job, _error_text(run.error))
    else:
        VideoJob.objects.filter(pk=job.pk, status=VideoJob.Status.BUILDING).update(
            progress_percentage=_build_progress(run.progress_percentage)
        )


def _start_export(job: VideoJob, gw: SupportsVideoGen) -> None:
    # Claim so only one caller ever starts the single export.
    claimed = VideoJob.objects.filter(
        pk=job.pk, status=VideoJob.Status.BUILDING, export_id=""
    ).update(status=VideoJob.Status.EXPORTING, progress_percentage=90.0)
    if not claimed:
        return

    try:
        resp = gw.start_export(job.project_id)
    except VideoGenError:
        VideoJob.objects.filter(
            pk=job.pk, status=VideoJob.Status.EXPORTING, export_id=""
        ).update(status=VideoJob.Status.BUILDING)
        raise

    VideoJob.objects.filter(pk=job.pk).update(export_id=resp.export_id)


def _poll_export(job: VideoJob, gw: SupportsVideoGen) -> None:
    if not job.export_id:
        return

    export = gw.get_export(job.project_id, job.export_id)
    state = _status_value(export.status)

    if state in _TERMINAL_OK:
        url = export.download_url
        if url:
            VideoJob.objects.filter(pk=job.pk, status=VideoJob.Status.EXPORTING).update(
                status=VideoJob.Status.READY,
                progress_percentage=100.0,
                download_url=url,
                download_url_expires_at=_expires_at(export),
            )
        else:
            # Succeeded but URL not yet populated — keep polling rather than
            # publishing a READY job with no download location.
            VideoJob.objects.filter(pk=job.pk, status=VideoJob.Status.EXPORTING).update(
                progress_percentage=99.0
            )
    elif state in _TERMINAL_FAIL:
        _fail(job, _error_text(export.error))
    else:
        VideoJob.objects.filter(pk=job.pk, status=VideoJob.Status.EXPORTING).update(
            progress_percentage=_export_progress(export.progress_percentage)
        )


def _refresh_download_url(job: VideoJob, gw: SupportsVideoGen) -> None:
    """On a READY job, re-fetch the export to return a freshly re-signed URL.

    This is a read (``get_project_export`` auto re-signs when near expiry), never a
    second export — so the MP4 stays downloadable indefinitely and is never billed
    again. A transient failure here must not break a READY read, so it is swallowed
    and the last known URL is kept.
    """
    if not (job.project_id and job.export_id):
        return
    try:
        export = gw.get_export(job.project_id, job.export_id)
    except VideoGenError:
        return
    if export.download_url:
        VideoJob.objects.filter(pk=job.pk, status=VideoJob.Status.READY).update(
            download_url=export.download_url,
            download_url_expires_at=_expires_at(export),
        )


def _fail(job: VideoJob, message: str) -> None:
    VideoJob.objects.filter(pk=job.pk).exclude(status=VideoJob.Status.READY).update(
        status=VideoJob.Status.FAILED, error_message=message[:2000]
    )
