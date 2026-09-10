"""VideoGen production pipeline for article videos.

This is the only place that talks to VideoGen. Every provider failure is
translated into a typed exception from :mod:`bakerydemo.videos.exceptions`, and
the produced video always has the required cheap shape:

    script-to-video (stock footage, 16:9)  ->  single 720p MP4 export

Exactly one workflow run and one export are issued per job.
"""

from __future__ import annotations

from videogen.errors import PollCancelledError
from videogen.errors import VideoGenError as SdkVideoGenError

from .client import get_videogen_client
from .constants import ASPECT_RATIO_16_9, EXPORT_QUALITY_720P, STOCK_VISUAL_STYLE
from .exceptions import (
    VideoExportError,
    VideoGenerationError,
    VideoGenProviderError,
    VideoProductionTimeout,
)
from .models import VideoJob, VideoJobStatus

# Progress bands: generation occupies the bulk of the run, export the tail.
_GENERATION_MAX_PROGRESS = 85
_EXPORT_START_PROGRESS = 90


def _save(job: VideoJob, **fields) -> None:
    for name, value in fields.items():
        setattr(job, name, value)
    job.save(update_fields=[*fields.keys(), "updated_at"])


def produce(job: VideoJob) -> VideoJob:
    """Run the full production pipeline for ``job``, updating it in place.

    On success the job ends ``READY`` with a download URL. On any provider
    failure a typed exception is raised (the caller records the failure); the
    job is never advanced to ``READY`` in that case.
    """
    client = get_videogen_client()

    # 1. Start the script-to-video workflow. This creates the single video
    #    project (stock footage, 16:9). Persist the handles immediately so the
    #    status endpoint can reflect the run as soon as it exists.
    try:
        started = client.workflows.script_to_video(
            script=job.script,
            visual_style=STOCK_VISUAL_STYLE,
            aspect_ratio=ASPECT_RATIO_16_9,
        )
    except SdkVideoGenError as exc:
        raise VideoGenerationError(
            f"Failed to start video generation: {exc}",
            status=getattr(exc, "status", None),
            body=getattr(exc, "body", None),
        ) from exc

    _save(
        job,
        workflow_run_id=started.get("workflow_run_id", ""),
        project_id=started.get("project_id", ""),
        status=VideoJobStatus.PROCESSING,
        progress_percentage=1,
    )

    # 2. Wait for generation to finish, mirroring provider progress.
    def _on_progress(pct: float) -> None:
        mapped = max(1, min(_GENERATION_MAX_PROGRESS, int(pct * 0.85)))
        if mapped != job.progress_percentage:
            _save(job, progress_percentage=mapped)

    try:
        client.poll_workflow_run(job.workflow_run_id, on_progress=_on_progress)
    except SdkVideoGenError as exc:
        raise VideoGenerationError(
            f"Video generation failed: {exc}",
            status=getattr(exc, "status", None),
            body=getattr(exc, "body", None),
        ) from exc
    except TimeoutError as exc:
        raise VideoProductionTimeout(f"Video generation timed out: {exc}") from exc
    except PollCancelledError as exc:
        raise VideoGenerationError(f"Video generation was cancelled: {exc}") from exc

    _save(job, progress_percentage=_GENERATION_MAX_PROGRESS)

    # 3. Export the finished project once, at 720p (never a second export).
    try:
        export_started = client.projects.export_project(
            project_id=job.project_id,
            quality=EXPORT_QUALITY_720P,
        )
    except SdkVideoGenError as exc:
        raise VideoExportError(
            f"Failed to start MP4 export: {exc}",
            status=getattr(exc, "status", None),
            body=getattr(exc, "body", None),
        ) from exc

    _save(
        job,
        export_id=export_started.get("export_id", ""),
        progress_percentage=_EXPORT_START_PROGRESS,
    )

    # 4. Wait for the export and capture the download handles.
    try:
        export = client.poll_project_export(job.project_id, job.export_id)
    except SdkVideoGenError as exc:
        raise VideoExportError(
            f"MP4 export failed: {exc}",
            status=getattr(exc, "status", None),
            body=getattr(exc, "body", None),
        ) from exc
    except TimeoutError as exc:
        raise VideoProductionTimeout(f"MP4 export timed out: {exc}") from exc
    except PollCancelledError as exc:
        raise VideoExportError(f"MP4 export was cancelled: {exc}") from exc

    _save(
        job,
        export_file_id=export.get("export_file_id") or "",
        download_url=export.get("download_url") or "",
        download_url_expires_at=export.get("download_url_expires_at"),
        status=VideoJobStatus.READY,
        progress_percentage=100,
        error="",
    )
    return job


def refresh_download_url(job: VideoJob) -> str | None:
    """Return a currently-valid signed download URL for a ready job.

    Signed URLs expire, so this re-fetches the export (which re-signs the URL)
    whenever the cached one is missing or close to expiry, keeping the video
    downloadable for as long as the article exists. This is a read-only call and
    is never billed.
    """
    if not job.is_ready:
        return None
    if job.download_url_is_fresh():
        return job.download_url

    client = get_videogen_client()
    try:
        if job.project_id and job.export_id:
            export = client.projects.get_project_export(
                project_id=job.project_id,
                export_id=job.export_id,
            )
            url = export.get("download_url") or ""
            expires_at = export.get("download_url_expires_at")
        else:
            url, expires_at = "", None

        if not url and job.export_file_id:
            # Fall back to hydrating the export file directly.
            from videogen import get_hydrated_file

            hydrated = get_hydrated_file(client, job.export_file_id)
            source = hydrated.get("download_source") or {}
            url = source.get("url") or ""
            expires_at = source.get("expires_at")
    except SdkVideoGenError as exc:
        raise VideoGenProviderError(
            f"Could not refresh the download URL: {exc}",
            status=getattr(exc, "status", None),
            body=getattr(exc, "body", None),
        ) from exc

    if url:
        _save(job, download_url=url, download_url_expires_at=expires_at)
    return url or (job.download_url or None)
