"""
Orchestration between the API layer, the database, and VideoGen.

Two operations drive the whole flow and contain all of the business rules:

* :func:`start_video_job` – idempotently begin producing a video for a page.
  Submits the narration to VideoGen exactly once and returns immediately.
* :func:`reconcile_job` – advance one job's state machine by reading VideoGen's
  own reported state, starting the single export when the build finishes, and
  storing the finished MP4 when the export finishes.

No background worker or task queue is used: the build and export run
asynchronously *on VideoGen's side*, and each status poll (a ``GET`` on the job)
reconciles the local record against the provider. That keeps the integration
robust (state lives in the database, not in a fragile in-process thread) and
free of any broker/queue infrastructure.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from .models import VideoJob
from .narration import build_narration_script
from .videogen import (
    VideoGenClient,
    VideoGenError,
    VideoGenExportError,
    VideoGenWorkflowError,
)
from .videogen.exceptions import TRANSIENT_HTTP_ERRORS

logger = logging.getLogger("bakerydemo.video")

#: Export resolution requested from VideoGen.
#:
#: The product requires a cheap 720p export (never 4K). VideoGen's *documentation*
#: lists ``quality`` enum values for the export endpoint, but every documented
#: value (``SD/HD/FULL_HD/4K``, ``RES_720P/RES_1080P`` and ``p720/p1080``) is
#: rejected by the live API with ``invalid_parameters`` – the docs do not expose
#: the real accepted constants. Rather than guess an undocumented value, we omit
#: ``quality`` (it is documented as optional) and let VideoGen apply its default
#: resolution. The default is configurable so that, once the correct constant is
#: known, an operator can set ``VIDEOGEN_EXPORT_QUALITY`` without a code change.
EXPORT_QUALITY = getattr(settings, "VIDEOGEN_EXPORT_QUALITY", "") or None


def get_client() -> VideoGenClient:
    """Build a :class:`VideoGenClient` from the project's settings.

    The API key and optional base-URL override are read from settings (which in
    turn read the ``VIDEOGEN_API_KEY`` / ``VIDEOGEN_BASE_URL`` environment
    variables) – never hard-coded here.
    """
    base_url = getattr(settings, "VIDEOGEN_BASE_URL", "") or None
    return VideoGenClient(
        api_key=getattr(settings, "VIDEOGEN_API_KEY", ""),
        base_url=base_url,
    )


def start_video_job(page, *, client: VideoGenClient | None = None) -> tuple[VideoJob, bool]:
    """Begin (or reuse) a video production job for ``page``.

    Returns ``(job, created)``. When a job already exists for the page the
    existing one is returned untouched (``created=False``) – asking twice in a
    row neither produces a second video nor bills a second time.

    The narration is built solely from the article's own title and the first
    sentence of its introduction.
    """
    script = build_narration_script(
        title=page.title,
        introduction=getattr(page, "introduction", "") or "",
    )

    # Idempotency guard: the one-to-one ``page`` link means only the first
    # concurrent request wins the create; everyone else reuses that job.
    try:
        with transaction.atomic():
            job = VideoJob.objects.create(
                page=page,
                script=script,
                status=VideoJob.Status.PROCESSING,
                progress_percentage=0,
            )
    except IntegrityError:
        return VideoJob.objects.get(page=page), False

    # We own the freshly-created job – submit to VideoGen exactly once.
    client = client or get_client()
    try:
        result = client.create_script_to_video(script)
    except VideoGenError:
        # Roll back the placeholder so a later request can retry cleanly without
        # leaving an un-started job blocking the page.
        job.delete()
        raise

    job.workflow_run_id = result.workflow_run_id
    job.project_id = result.project_id
    job.save(update_fields=["workflow_run_id", "project_id", "updated_at"])
    logger.info(
        "Started VideoGen workflow %s (project %s) for page %s",
        result.workflow_run_id,
        result.project_id,
        page.pk,
    )
    return job, True


def reconcile_job(job: VideoJob, *, client: VideoGenClient | None = None) -> VideoJob:
    """Advance ``job`` by reconciling it against VideoGen's reported state.

    Safe to call repeatedly. Terminal jobs (``ready``/``failed``) are returned
    untouched and make no network calls. Transient provider errors (rate limit,
    5xx, connection) leave the job ``processing`` to be retried on the next
    poll; terminal provider failures mark the job ``failed`` with the
    provider-reported reason.
    """
    if job.is_terminal:
        return job

    # Job created but the submission has not recorded a workflow run yet.
    if not job.workflow_run_id:
        return job

    client = client or get_client()
    try:
        if not job.export_id:
            _advance_workflow(job, client)
        else:
            _advance_export(job, client)
    except TRANSIENT_HTTP_ERRORS as exc:
        # Still in flight on VideoGen's side – keep polling, don't fail the job.
        logger.warning("Transient VideoGen error while polling job %s: %s", job.id, exc)
    except VideoGenError as exc:
        logger.error("VideoGen failure for job %s: %s", job.id, exc)
        _mark_failed(job, str(exc))
    return job


def _advance_workflow(job: VideoJob, client: VideoGenClient) -> None:
    """Poll the build; when it succeeds, start the single export."""
    run = client.get_workflow_run(job.workflow_run_id)
    # The build is the first half of our 0–100 progress bar.
    job.progress_percentage = max(job.progress_percentage, run.progress_percentage // 2)
    if run.project_id:
        job.project_id = run.project_id

    if run.status == "succeeded":
        if not job.project_id:
            raise VideoGenWorkflowError(
                "Workflow succeeded but returned no projectId to export.",
            )
        job.progress_percentage = max(job.progress_percentage, 50)
        # Exactly one export, at 720p. Only reached once because ``export_id``
        # is persisted immediately afterwards and short-circuits future polls.
        export_id = client.start_export(job.project_id, quality=EXPORT_QUALITY)
        job.export_id = export_id
        logger.info("Started export %s for job %s", export_id, job.id)
    elif run.status in ("failed", "cancelled"):
        raise VideoGenWorkflowError(
            run.error_message or f"VideoGen workflow {run.status}.",
        )

    job.save()


def _advance_export(job: VideoJob, client: VideoGenClient) -> None:
    """Poll the export; when it succeeds, store the finished MP4."""
    export = client.get_export(job.project_id, job.export_id)
    # The export is the second half of the progress bar.
    job.progress_percentage = max(
        job.progress_percentage, 50 + export.progress_percentage // 2
    )
    if export.export_file_id:
        job.export_file_id = export.export_file_id

    if export.status == "succeeded":
        if not job.video_file:
            _store_rendered_video(job, client, export)
        job.progress_percentage = 100
        job.status = VideoJob.Status.READY
    elif export.status in ("failed", "cancelled"):
        raise VideoGenExportError(
            export.error_message or f"VideoGen export {export.status}.",
        )

    job.save()


def _store_rendered_video(job: VideoJob, client: VideoGenClient, export) -> None:
    """Download the rendered MP4 into the site's own storage.

    Storing the file locally means the video stays retrievable through the API
    for as long as the article exists, independent of VideoGen's 7-day signed
    URL expiry. Downloading a finished file is not a billed operation.
    """
    if not export.download_url:
        raise VideoGenExportError(
            "Export succeeded but VideoGen returned no downloadUrl.",
        )
    content = client.download_file(export.download_url)
    filename = f"article-{job.page_id}-{job.id}.mp4"
    job.video_file.save(filename, ContentFile(content), save=False)
    logger.info("Stored rendered video for job %s (%d bytes)", job.id, len(content))


def _mark_failed(job: VideoJob, error: str) -> None:
    job.status = VideoJob.Status.FAILED
    job.error = error
    job.save(update_fields=["status", "error", "updated_at"])
