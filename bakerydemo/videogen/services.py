"""Orchestration for turning an article into a video.

This module owns the whole flow and its idempotency guarantees:

* :func:`get_or_create_video_job` — accept a request for an article's video,
  creating at most one job per article (so "ask twice" never bills twice), and
  hand it off to be produced in the background.
* :func:`run_video_job` — drive one job through VideoGen end to end: create the
  ``script-to-video`` run, wait for it, export once at 720p, wait for it,
  download the MP4 and store it on the site. It is resumable: identifiers are
  persisted as they are obtained, so a re-run never creates a second video.

Background execution uses a plain daemon thread — the project has no task queue,
broker or worker process, and the task forbids introducing one. :func:`enqueue`
is the single seam where that happens, so it can be swapped or patched (the tests
do the latter).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction

from .client import VideoGenClient
from .exceptions import VideoGenError
from .models import VideoJob, VideoJobStatus
from .narration import build_narration_script

logger = logging.getLogger("bakerydemo.videogen")

# Progress budget: the workflow run drives 0-70%, the export 70-99%, and a fully
# stored MP4 is 100%. This lets a caller watch a single monotonic number.
_WORKFLOW_PROGRESS_CEILING = 70
_EXPORT_PROGRESS_FLOOR = 70
_EXPORT_PROGRESS_CEILING = 99


# ---------------------------------------------------------------------------
# Creating / accepting jobs
# ---------------------------------------------------------------------------


def build_job_narration(page) -> str:
    """Return the narration script for ``page`` (a BlogPage-like object)."""
    return build_narration_script(page.title, getattr(page, "introduction", "") or "")


def get_or_create_video_job(page) -> tuple[VideoJob, bool]:
    """Return ``(job, created)`` for ``page``, producing it if newly created.

    Idempotent: the one-to-one link to the page means a second request for the
    same article returns the existing job instead of starting another video.
    """
    with transaction.atomic():
        try:
            job = VideoJob.objects.get(page=page)
            created = False
        except VideoJob.DoesNotExist:
            job = VideoJob.objects.create(
                page=page,
                narration_script=build_job_narration(page),
                status=VideoJobStatus.PENDING,
            )
            created = True

    if created:
        # Only kick off production once the row is safely committed, so the
        # background worker can read it.
        transaction.on_commit(lambda: enqueue(job.pk))

    return job, created


# ---------------------------------------------------------------------------
# Background execution seam
# ---------------------------------------------------------------------------


def enqueue(job_pk: int) -> None:
    """Run :func:`run_video_job` for ``job_pk`` in a background daemon thread."""
    thread = threading.Thread(
        target=_run_in_thread,
        args=(job_pk,),
        name=f"videogen-job-{job_pk}",
        daemon=True,
    )
    thread.start()


def _run_in_thread(job_pk: int) -> None:
    # A worker thread owns its own DB connection; close it when done so the
    # connection is not leaked back into the pool half-used.
    try:
        run_video_job(job_pk)
    finally:
        close_old_connections()


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def run_video_job(job_pk: int, *, client: VideoGenClient | None = None) -> VideoJob:
    """Drive one job to a terminal state. Never raises for provider failures.

    Returns the refreshed job. Provider failures are recorded on the job as
    ``status=failed`` with a human-readable ``error`` (and machine ``error_code``
    when the provider supplied one).
    """
    job = VideoJob.objects.get(pk=job_pk)
    if job.status == VideoJobStatus.READY:
        return job

    _update(job, status=VideoJobStatus.PROCESSING, error="", error_code="")

    try:
        client = client or VideoGenClient()
        _produce(job, client)
    except VideoGenError as exc:
        logger.warning("VideoGen job %s failed: %s", job.uuid, exc)
        _update(
            job,
            status=VideoJobStatus.FAILED,
            error=str(exc),
            error_code=getattr(exc, "code", "") or "",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error in VideoGen job %s", job.uuid)
        _update(
            job,
            status=VideoJobStatus.FAILED,
            error=f"Unexpected error while producing the video: {exc}",
        )

    job.refresh_from_db()
    return job


def _produce(job: VideoJob, client: VideoGenClient) -> None:
    # 1. Start the workflow run (only if we have not already).
    if not job.workflow_run_id:
        run = client.create_script_to_video(script=job.narration_script)
        _update(
            job,
            workflow_run_id=run["workflowRunId"],
            project_id=run["projectId"],
        )

    # 2. Wait for the run to finish.
    client.poll_workflow_run(
        job.workflow_run_id,
        on_progress=lambda state: _update(
            job,
            progress_percentage=_scale(
                state.get("progressPercentage"), 0, _WORKFLOW_PROGRESS_CEILING
            ),
        ),
    )

    # 3. Export exactly once (720p / STANDARD), only if not already started.
    if not job.export_id:
        export = client.export_project(job.project_id, quality="STANDARD")
        _update(job, export_id=export["exportId"])

    # 4. Wait for the export to finish.
    export = client.poll_project_export(
        job.project_id,
        job.export_id,
        on_progress=lambda state: _update(
            job,
            progress_percentage=_scale(
                state.get("progressPercentage"),
                _EXPORT_PROGRESS_FLOOR,
                _EXPORT_PROGRESS_CEILING,
            ),
        ),
    )

    # 5. Download the finished MP4 and store it on the site.
    download_url = export.get("downloadUrl")
    if not download_url:
        from .exceptions import VideoGenExportError

        raise VideoGenExportError(
            "Export succeeded but no downloadUrl was provided by VideoGen."
        )
    content = client.download(download_url)

    job.export_file_id = export.get("exportFileId") or ""
    job.video_file.save(f"{job.uuid}.mp4", ContentFile(content), save=False)
    _update(
        job,
        status=VideoJobStatus.READY,
        progress_percentage=100,
        error="",
        error_code="",
        # video_file / export_file_id already set on the instance above
        _extra_fields=("video_file", "export_file_id"),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _update(
    job: VideoJob, *, _extra_fields: tuple[str, ...] = (), **fields: Any
) -> None:
    """Assign ``fields`` to ``job`` and persist just those columns."""
    for name, value in fields.items():
        setattr(job, name, value)
    update_fields = set(fields) | set(_extra_fields) | {"updated_at"}
    job.save(update_fields=list(update_fields))


def _scale(value: Any, floor: int, ceiling: int) -> int:
    """Map a provider 0-100 progress value into the ``[floor, ceiling]`` band."""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    return int(floor + (ceiling - floor) * (pct / 100.0))
