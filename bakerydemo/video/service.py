"""Orchestration for producing an article video with VideoGen.

The public entry point is :func:`start_video_job`, which is idempotent per page
and kicks the actual production off in a background thread so the HTTP ``POST``
can return immediately. The production itself — :func:`run_pipeline` — is a
plain synchronous function so it can be exercised directly (and against a mocked
client) in tests.

The pipeline is exactly: **script-to-video -> poll -> export once (720p) ->
poll -> download the MP4 -> store it on the site**. It never produces a second
video, exports twice, or generates any standalone media.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from django.core.files.base import ContentFile
from django.db import connection, transaction

from .exceptions import VideoGenError, VideoGenJobFailedError
from .models import VideoJob, VideoJobStatus
from .narration import build_script_for_page
from .videogen import VideoGenClient

logger = logging.getLogger(__name__)

# Terminal states reported by VideoGen for both workflow runs and exports.
_TERMINAL_OK = "succeeded"
_TERMINAL_BAD = {"failed", "cancelled"}

# Poll cadence and an overall safety ceiling so a stuck provider job cannot pin
# a thread forever.
POLL_INTERVAL_SECONDS = 3.0
MAX_POLL_SECONDS = 30 * 60

# Export quality tier. VideoGen exposes STANDARD < HIGH < FULL_HIGH <
# ULTRA_HIGH; STANDARD is the lowest (720p-class) tier — the cheapest option and
# never 4K, which is exactly what the spend limits require.
EXPORT_QUALITY_720P = "STANDARD"

ClientFactory = Callable[[], VideoGenClient]
Runner = Callable[[str], None]


def _default_client_factory() -> VideoGenClient:
    return VideoGenClient.from_settings()


def start_video_job(
    page,
    *,
    client_factory: ClientFactory = _default_client_factory,
    runner: Runner | None = None,
) -> tuple[VideoJob, bool]:
    """Return the video job for ``page``, starting production if needed.

    Idempotency: at most one job exists per page. If a live (pending/processing/
    ready) job already exists, it is returned untouched and no new production is
    started — so a repeated request never yields a second, separately billed
    video. A previously *failed* job is reset and retried in place.

    Returns ``(job, started)`` where ``started`` indicates whether a new
    production run was kicked off by this call.
    """
    script = build_script_for_page(page)

    with transaction.atomic():
        job, created = VideoJob.objects.select_for_update().get_or_create(
            page=page,
            defaults={"script": script, "status": VideoJobStatus.PENDING},
        )
        should_start = created
        if not created and job.status == VideoJobStatus.FAILED:
            job.reset_for_retry(script)
            should_start = True

    if should_start:
        _launch(job.pk, client_factory=client_factory, runner=runner)

    return job, should_start


def _launch(
    job_id,
    *,
    client_factory: ClientFactory,
    runner: Runner | None,
) -> None:
    if runner is not None:
        runner(str(job_id))
        return

    def _target() -> None:
        # Use a fresh DB connection for this thread and always release it.
        connection.close()
        try:
            run_pipeline(str(job_id), client=client_factory())
        finally:
            connection.close()

    thread = threading.Thread(
        target=_target,
        name=f"videogen-job-{job_id}",
        daemon=True,
    )
    thread.start()


def _progress(job: VideoJob, value: int) -> None:
    job.progress_percentage = max(0, min(100, value))
    job.save(update_fields=["progress_percentage", "updated_at"])


def _provider_error_text(payload: dict, fallback: str) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("message") or fallback
    if isinstance(error, str) and error.strip():
        return error.strip()
    return fallback


def _poll(
    fetch: Callable[[], dict],
    *,
    map_progress: Callable[[int], None],
    poll_interval: float,
    deadline: float,
    what: str,
) -> dict:
    """Poll ``fetch`` until VideoGen reports a terminal status.

    Returns the final (succeeded) payload, or raises ``VideoGenJobFailedError``
    if the provider reports failure/cancellation or the deadline is exceeded.
    """
    while True:
        payload = fetch()
        status = (payload.get("status") or "").lower()
        progress = payload.get("progressPercentage")
        if isinstance(progress, (int, float)):
            map_progress(int(progress))

        if status == _TERMINAL_OK:
            return payload
        if status in _TERMINAL_BAD:
            raise VideoGenJobFailedError(
                _provider_error_text(payload, f"VideoGen {what} {status}."),
                code=status,
                body=payload,
            )

        if time.monotonic() >= deadline:
            raise VideoGenJobFailedError(
                f"VideoGen {what} did not finish within the allotted time."
            )
        time.sleep(poll_interval)


def run_pipeline(
    job_id: str,
    *,
    client: VideoGenClient | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    max_poll_seconds: float = MAX_POLL_SECONDS,
) -> None:
    """Run the full production pipeline for one job (synchronous).

    Any provider failure is caught and recorded on the job as a typed error;
    the exception itself is a :class:`VideoGenError` subclass throughout.
    """
    try:
        job = VideoJob.objects.get(pk=job_id)
    except VideoJob.DoesNotExist:
        logger.warning("VideoGen pipeline: job %s no longer exists", job_id)
        return

    client = client or VideoGenClient.from_settings()
    deadline = time.monotonic() + max_poll_seconds

    try:
        job.status = VideoJobStatus.PROCESSING
        job.save(update_fields=["status", "updated_at"])

        if not job.project_id:
            # 1. Start the script-to-video workflow (the one and only video).
            run = client.create_script_to_video(script=job.script)
            job.workflow_run_id = run.get("workflowRunId", "") or ""
            job.project_id = run.get("projectId", "") or ""
            job.save(update_fields=["workflow_run_id", "project_id", "updated_at"])

            # 2. Poll the workflow run to completion (0-50% of progress).
            final_run = _poll(
                lambda: client.get_workflow_run(job.workflow_run_id),
                map_progress=lambda p: _progress(job, min(49, p // 2)),
                poll_interval=poll_interval,
                deadline=deadline,
                what="workflow run",
            )
            # projectId is authoritative on the run payload.
            job.project_id = (
                final_run.get("projectId", job.project_id) or job.project_id
            )
            job.save(update_fields=["project_id", "updated_at"])
        else:
            # Resuming a job whose workflow already produced the project: never
            # re-run the workflow (that would create — and bill for — a second
            # video). Pick up at the export step against the existing project.
            logger.info(
                "VideoGen pipeline resuming export for existing project %s (job %s)",
                job.project_id,
                job.pk,
            )
        _progress(job, 50)

        # 3. Export the finished project exactly once, at 720p.
        export = client.export_project(job.project_id, quality=EXPORT_QUALITY_720P)
        job.export_id = export.get("exportId", "") or ""
        job.save(update_fields=["export_id", "updated_at"])

        # 4. Poll the export to completion (50-100% of overall progress).
        final_export = _poll(
            lambda: client.get_project_export(job.project_id, job.export_id),
            map_progress=lambda p: _progress(job, 50 + min(50, p // 2)),
            poll_interval=poll_interval,
            deadline=deadline,
            what="export",
        )
        job.export_file_id = final_export.get("exportFileId", "") or ""

        download_url = final_export.get("downloadUrl")
        if not download_url:
            raise VideoGenJobFailedError(
                "VideoGen reported the export succeeded but returned no download URL.",
                body=final_export,
            )

        # 5. Download the MP4 once and store it on the site.
        data = client.download_file(download_url)
        filename = f"page-{job.page_id}-{job.pk}.mp4"
        job.video_file.save(filename, ContentFile(data), save=False)
        job.status = VideoJobStatus.READY
        job.progress_percentage = 100
        job.error = ""
        job.save(
            update_fields=[
                "video_file",
                "export_file_id",
                "status",
                "progress_percentage",
                "error",
                "updated_at",
            ]
        )
        logger.info(
            "VideoGen pipeline succeeded for job %s (page %s)",
            job.pk,
            job.page_id,
        )
    except VideoGenError as exc:
        logger.warning("VideoGen pipeline failed for job %s: %s", job.pk, exc)
        job.mark_failed(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error in VideoGen pipeline for job %s", job.pk)
        job.mark_failed(f"Unexpected error while producing the video: {exc}")
