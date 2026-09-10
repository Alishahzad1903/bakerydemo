"""Orchestrates producing one video for an article.

The public entry points are :func:`ensure_production_configured` (a fast,
side-effect-free guard the API calls *before* creating a job) and
:func:`start_production` (fire-and-forget background worker).

There is no task queue / broker in this project (and the brief forbids adding
one), so production runs in a daemon thread. The job row in the database is the
durable state the status endpoint reads; the thread only advances that row.

Two knobs the mandated spend-shape requires are *not* documented by the
VideoGen ``api`` skill, so there is no compliant value to hard-code:

* **Stock-footage visual style.** The skill documents exactly one workflow
  ``visualStyle`` — ``{"type": "AI_IMAGE", ...}`` — which the spend policy
  forbids ("Never AI-generated imagery"). No stock-footage visual style is
  documented, and inventing one is prohibited.
* **720p / 16:9 export.** ``POST /v1/projects/{id}/export`` is documented as
  taking no request parameters, so output resolution and aspect ratio cannot be
  requested; a 4K default cannot be ruled out.

Rather than guess, these are read from settings (``VIDEOGEN_VISUAL_STYLE`` and
``VIDEOGEN_EXPORT_OPTIONS``). Until an operator supplies values that the skill
covers, production refuses to start with a typed
:class:`VideoGenCapabilityUnavailable`, so the billed account is never charged
for a video whose shape violates the spend policy.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import connection

from .videogen import (
    VideoGenAPIError,
    VideoGenCapabilityUnavailable,
    VideoGenConfigurationError,
    VideoGenError,
    VideoGenProductionFailed,
    get_client,
)
from .videogen.client import DEFAULT_BASE_URL

logger = logging.getLogger("bakerydemo.videos")


@dataclass(frozen=True)
class ProductionConfig:
    """Resolved, spend-compliant shape for a single production."""

    visual_style: dict
    export_options: dict
    quality: str | None = None
    remix_actions: list = field(default_factory=list)


def resolve_production_config() -> ProductionConfig:
    """Resolve and validate the production shape, or raise a typed error.

    Raises:
        VideoGenConfigurationError: no API key configured.
        VideoGenCapabilityUnavailable: a mandated shape (stock footage, or a
            720p/16:9 export) is not configured because the skill does not
            document how to request it.
    """
    if not (getattr(settings, "VIDEOGEN_API_KEY", "") or ""):
        raise VideoGenConfigurationError(
            "VIDEOGEN_API_KEY is not set; cannot produce a video."
        )

    visual_style = getattr(settings, "VIDEOGEN_VISUAL_STYLE", None)
    if not visual_style:
        raise VideoGenCapabilityUnavailable(
            "No stock-footage visual style is configured (VIDEOGEN_VISUAL_STYLE). "
            "The spend policy requires stock footage and forbids AI-generated "
            "imagery, but the VideoGen API skill documents only "
            "visualStyle {'type': 'AI_IMAGE'} for workflows. Refusing to "
            "produce a video rather than bill the account for AI imagery."
        )

    export_options = getattr(settings, "VIDEOGEN_EXPORT_OPTIONS", None)
    if export_options is None:
        raise VideoGenCapabilityUnavailable(
            "No export shape is configured (VIDEOGEN_EXPORT_OPTIONS). The spend "
            "policy requires a single 720p, 16:9 export, but the VideoGen API "
            "skill documents no request parameters for POST "
            "/v1/projects/{id}/export, so resolution and aspect ratio cannot be "
            "requested. Refusing to produce a video rather than risk a non-720p "
            "(e.g. 4K) export that cannot be corrected without a second, billed "
            "export."
        )

    return ProductionConfig(
        visual_style=dict(visual_style),
        export_options=dict(export_options),
        quality=getattr(settings, "VIDEOGEN_QUALITY", None) or None,
        remix_actions=list(getattr(settings, "VIDEOGEN_REMIX_ACTIONS", []) or []),
    )


def ensure_production_configured() -> ProductionConfig:
    """Guard used by the API before creating a job. Returns the config or raises."""
    return resolve_production_config()


def start_production(job_id: str) -> None:
    """Kick off production for ``job_id`` in a daemon thread (non-blocking)."""
    thread = threading.Thread(
        target=_run_production,
        args=(str(job_id),),
        name=f"videogen-production-{job_id}",
        daemon=True,
    )
    thread.start()


# -- internals -----------------------------------------------------------


def _set_progress(job, status: str | None, percentage: int | None) -> None:
    fields = ["updated_at"]
    if status is not None and job.status != status:
        job.status = status
        fields.append("status")
    if percentage is not None:
        # Never move progress backwards.
        capped = max(0, min(100, int(percentage)))
        if capped > job.progress_percentage:
            job.progress_percentage = capped
            fields.append("progress_percentage")
    if len(fields) > 1:
        job.save(update_fields=fields)


def _scaled_progress(payload: dict, low: int, high: int) -> int:
    """Map a provider 0-100 progress into the [low, high] slice of our bar."""
    raw = payload.get("progressPercentage")
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        raw = 0.0
    raw = max(0.0, min(100.0, raw))
    return int(low + (high - low) * (raw / 100.0))


def _download_source_url(file_info: dict) -> str | None:
    source = file_info.get("downloadSource") or {}
    url = source.get("url")
    return url or None


def _run_production(job_id: str) -> None:
    from .models import VideoJob

    job = VideoJob.objects.filter(pk=job_id).select_related("page").first()
    if job is None:
        logger.warning("VideoJob %s vanished before production started", job_id)
        return

    try:
        config = resolve_production_config()
        client = get_client()

        _set_progress(job, VideoJob.Status.RUNNING, 5)

        run = client.create_script_to_video(
            script=job.script,
            visual_style=config.visual_style,
            quality=config.quality,
            remix_actions=config.remix_actions,
        )
        job.workflow_run_id = run.get("workflowRunId", "") or ""
        job.project_id = run.get("projectId", "") or ""
        job.save(update_fields=["workflow_run_id", "project_id", "updated_at"])

        if not job.workflow_run_id:
            raise VideoGenAPIError(
                "script-to-video response did not include a workflowRunId.",
                payload=run,
            )

        run_final = client.poll_workflow_run(
            job.workflow_run_id,
            on_progress=lambda p: _set_progress(job, None, _scaled_progress(p, 5, 55)),
        )
        project_id = run_final.get("projectId") or job.project_id
        if not project_id:
            raise VideoGenAPIError(
                "workflow run succeeded but no projectId was returned.",
                payload=run_final,
            )
        job.project_id = project_id
        _set_progress(job, VideoJob.Status.RUNNING, 60)

        export_start = client.export_project(project_id, config.export_options)
        job.export_id = export_start.get("exportId", "") or ""
        job.save(update_fields=["project_id", "export_id", "updated_at"])
        if not job.export_id:
            raise VideoGenAPIError(
                "export response did not include an exportId.",
                payload=export_start,
            )

        export_final = client.poll_project_export(
            project_id,
            job.export_id,
            on_progress=lambda p: _set_progress(job, None, _scaled_progress(p, 60, 95)),
        )

        download_url = export_final.get("downloadUrl")
        export_file_id = export_final.get("exportFileId") or ""
        if not download_url and export_file_id:
            # Signed URL missing/expired — re-hydrate to obtain a fresh one.
            hydrated = client.hydrate_file(export_file_id)
            download_url = _download_source_url(hydrated)
        if not download_url:
            raise VideoGenProductionFailed(
                "Export finished but VideoGen returned no download URL.",
                status="succeeded",
                provider_error=export_final,
            )

        content = client.download_bytes(download_url)
        job.export_file_id = export_file_id
        job.video_file.save(f"{job.id}.mp4", ContentFile(content), save=False)
        job.status = VideoJob.Status.SUCCEEDED
        job.progress_percentage = 100
        job.error = ""
        job.save(
            update_fields=[
                "export_file_id",
                "video_file",
                "status",
                "progress_percentage",
                "error",
                "updated_at",
            ]
        )
        logger.info("VideoJob %s succeeded for page %s", job.id, job.page_id)

    except VideoGenError as exc:
        logger.warning("VideoJob %s failed: %s", job_id, exc)
        _safe_fail(job, str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("VideoJob %s crashed unexpectedly", job_id)
        _safe_fail(job, f"Unexpected error during production: {exc}")
    finally:
        # Release this thread's DB connection.
        connection.close()


def _safe_fail(job, message: str) -> None:
    try:
        job.mark_failed(message)
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "Could not mark VideoJob %s as failed", getattr(job, "id", "?")
        )


__all__ = [
    "ProductionConfig",
    "resolve_production_config",
    "ensure_production_configured",
    "start_production",
    "DEFAULT_BASE_URL",
]
