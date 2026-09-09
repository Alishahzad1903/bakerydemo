"""Orchestration for turning an article into a narrated MP4.

The public entry points are:

* :func:`start_video_job` — idempotently create (or reuse) the job for a page.
* :func:`enqueue_video_job` — run a pending job's pipeline in the background.
* :func:`run_video_job` — the pipeline itself (start workflow → export →
  download → store), usable synchronously (e.g. from a test or command).

There is no external task queue in this project (and none may be introduced),
so background work runs in a daemon thread. The single source of truth for a
job's state is the :class:`~bakerydemo.videos.models.VideoJob` row, which the
GET endpoint reads.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import IntegrityError, connections, transaction

from .client import VideoGenClient
from .exceptions import (
    VideoGenConfigurationError,
    VideoGenError,
    VideoGenResponseError,
)
from .models import ACTIVE_STATUSES, VideoJob, VideoJobStatus
from .narration import build_narration_script

logger = logging.getLogger("bakerydemo.videos")

# Progress checkpoints (0-100) mapped across the pipeline stages so the caller
# sees steady forward movement.
_PROGRESS_WORKFLOW_START = 5
_PROGRESS_WORKFLOW_END = 70
_PROGRESS_EXPORT_START = 72
_PROGRESS_EXPORT_END = 95
_PROGRESS_DOWNLOAD = 97


def start_video_job(page) -> tuple[VideoJob, bool]:
    """Return the video job for ``page``, creating one only if needed.

    Idempotent: if an in-flight or already-produced job exists for the article,
    it is returned unchanged (``created=False``) and no new video is produced or
    billed. Returns ``(job, created)``.
    """
    with transaction.atomic():
        existing = (
            VideoJob.objects.select_for_update()
            .filter(page=page, status__in=list(ACTIVE_STATUSES))
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return existing, False

        script = build_narration_script(
            page, max_words=int(getattr(settings, "VIDEOGEN_MAX_SCRIPT_WORDS", 30))
        )
        try:
            job = VideoJob.objects.create(
                page=page,
                status=VideoJobStatus.PENDING,
                script=script,
            )
            return job, True
        except IntegrityError:
            # A concurrent request won the race and created the active job;
            # reuse it rather than producing a second video.
            job = (
                VideoJob.objects.filter(page=page, status__in=list(ACTIVE_STATUSES))
                .order_by("-created_at")
                .first()
            )
            if job is None:  # pragma: no cover - extremely unlikely
                raise
            return job, False


def enqueue_video_job(job_id) -> None:
    """Run ``job_id``'s pipeline in a daemon thread."""
    thread = threading.Thread(
        target=_run_job_safely,
        args=(job_id,),
        name=f"videogen-job-{job_id}",
        daemon=True,
    )
    thread.start()


def _run_job_safely(job_id) -> None:
    try:
        run_video_job(job_id)
    except Exception:
        logger.exception("VideoGen job %s crashed unexpectedly", job_id)
        _mark_failed(job_id, "The video could not be produced due to an internal error.")
    finally:
        # A background thread gets its own DB connection; close it so we don't
        # leak connections for the life of the process.
        connections.close_all()


def run_video_job(job_id) -> None:
    """Drive one job from ``pending`` to ``ready`` (or ``failed``).

    Only a job in the ``pending`` state is executed; the transition to
    ``processing`` is a conditional update, so even if this is called twice the
    provider work runs at most once.
    """
    claimed = VideoJob.objects.filter(
        pk=job_id, status=VideoJobStatus.PENDING
    ).update(status=VideoJobStatus.PROCESSING, progress_percentage=_PROGRESS_WORKFLOW_START, error="")
    if not claimed:
        logger.info("VideoGen job %s is not pending; skipping execution.", job_id)
        return

    job = VideoJob.objects.get(pk=job_id)

    deadline = time.monotonic() + float(getattr(settings, "VIDEOGEN_TIMEOUT_SECONDS", 1800))

    try:
        # Resolve request params (incl. the required visualStyle) and the client
        # before doing any provider work, so a misconfiguration (missing API key
        # or visual style) fails fast with a clear, typed error and never calls
        # the paid API.
        script_params = _script_to_video_params()
        client = VideoGenClient()
    except VideoGenError as exc:
        logger.warning("VideoGen job %s failed: %s", job_id, exc)
        _mark_failed(job_id, str(exc))
        return

    try:
        # 1. Start the script-to-video workflow (narration = the article's words).
        started = client.create_script_to_video(job.script, extra=script_params)
        run_id = started["workflowRunId"]
        project_id = started.get("projectId") or ""
        _set(
            job_id,
            videogen_workflow_run_id=run_id,
            videogen_project_id=project_id,
            progress_percentage=_PROGRESS_WORKFLOW_START + 1,
        )
        logger.info("VideoGen job %s started workflow run %s", job_id, run_id)

        # 2. Wait for the workflow to finish producing the project.
        run = client.wait_for_workflow(
            run_id,
            on_progress=lambda p: _set_progress(
                job_id, _scale(p, _PROGRESS_WORKFLOW_START, _PROGRESS_WORKFLOW_END)
            ),
            deadline=deadline,
        )
        project_id = run.get("projectId") or project_id
        if not project_id:
            raise VideoGenResponseError(
                "The workflow finished without a projectId to export.", body=run
            )
        _set(
            job_id,
            videogen_project_id=project_id,
            progress_percentage=_PROGRESS_WORKFLOW_END,
        )

        # 3. Export the finished project to MP4 (exactly one export).
        export_started = client.export_project(
            project_id, extra=getattr(settings, "VIDEOGEN_EXPORT_PARAMS", None) or None
        )
        export_id = export_started["exportId"]
        _set(
            job_id,
            videogen_export_id=export_id,
            progress_percentage=_PROGRESS_EXPORT_START,
        )
        logger.info("VideoGen job %s started export %s", job_id, export_id)

        # 4. Wait for the export, then download the MP4 into our own storage.
        export = client.wait_for_export(
            project_id,
            export_id,
            on_progress=lambda p: _set_progress(
                job_id, _scale(p, _PROGRESS_EXPORT_START, _PROGRESS_EXPORT_END)
            ),
            deadline=deadline,
        )
        download_url = export.get("downloadUrl")
        if not download_url:
            raise VideoGenResponseError(
                "The export finished without a downloadUrl.", body=export
            )
        _set(
            job_id,
            videogen_export_file_id=export.get("exportFileId") or "",
            progress_percentage=_PROGRESS_DOWNLOAD,
        )

        _download_to_storage(job_id, client, download_url)

        _set(
            job_id,
            status=VideoJobStatus.READY,
            progress_percentage=100,
            error="",
        )
        logger.info("VideoGen job %s is ready", job_id)

    except VideoGenError as exc:
        logger.warning("VideoGen job %s failed: %s", job_id, exc)
        _mark_failed(job_id, str(exc))
    finally:
        client.close()


def _download_to_storage(job_id, client: VideoGenClient, download_url: str) -> None:
    job = VideoJob.objects.get(pk=job_id)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            client.download_to(download_url, tmp)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as handle:
            job.video_file.save(f"{job.pk}.mp4", File(handle), save=True)
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)


def _mark_failed(job_id, message: str) -> None:
    VideoJob.objects.filter(pk=job_id).exclude(
        status=VideoJobStatus.READY
    ).update(status=VideoJobStatus.FAILED, error=message)


def _set(job_id, **fields) -> None:
    VideoJob.objects.filter(pk=job_id).update(**fields)


def _set_progress(job_id, value: int) -> None:
    VideoJob.objects.filter(pk=job_id).update(progress_percentage=value)


def _scale(percent: float, low: int, high: int) -> int:
    percent = max(0.0, min(100.0, float(percent)))
    return int(low + (high - low) * percent / 100.0)


def _script_to_video_params() -> dict:
    """Assemble the script-to-video request body extras (minus the script).

    VideoGen requires a ``visualStyle``; we take it from ``VIDEOGEN_VISUAL_STYLE``
    (plus any extra ``VIDEOGEN_SCRIPT_PARAMS``). If no visual style is configured
    we fail fast with a typed configuration error rather than send a request that
    the provider will reject — and, crucially, rather than guess a value that
    might request AI imagery or spend the single billed video on an unverified
    configuration.
    """
    params = dict(getattr(settings, "VIDEOGEN_SCRIPT_PARAMS", None) or {})
    visual_style = getattr(settings, "VIDEOGEN_VISUAL_STYLE", None)
    if visual_style and "visualStyle" not in params:
        params["visualStyle"] = visual_style
    if not params.get("visualStyle"):
        raise VideoGenConfigurationError(
            "No VideoGen visual style is configured. Set VIDEOGEN_VISUAL_STYLE to "
            "the stock-footage visual style for your account (see docs/videogen.md)."
        )
    return params
