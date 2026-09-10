"""Typed-exception boundary over the VideoGen SDK.

This is the only module in the site that talks to the ``videogen`` SDK. It
translates every provider/transport failure into a :class:`VideoGenError`
subclass (per the integration's "surface provider failures as typed
exceptions" requirement) and hands back plain, framework-agnostic values so no
SDK model (and no ``UNSET`` sentinel) ever escapes this layer.

Only the cheap "one voice over stock footage" shape is ever requested:
    * visual style STOCK (stock footage/images only, never AI-generated),
    * no actor/avatar (voiceover only),
    * 16:9 aspect ratio,
    * a single 720p export.

The SDK performs no retries of its own; the polling loop that drives a job to
completion lives in :mod:`bakerydemo.videos.producer`, not here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import ValidationError
from videogen import VideogenClient
from videogen.core import UNSET, ApiError, UnsetType
from videogen.models import ExportProjectRequestDict, ScriptToVideoRequestDict
from videogen.models.enums import ExportProjectQuality, WorkflowVisualStyleType

from .client import get_client
from .exceptions import (
    VideoGenAPIError,
    VideoGenUnavailableError,
    VideoGenUnreadableError,
)

logger = logging.getLogger(__name__)

# JobStatus wire values (videogen/models/enums/job_status.py). The enum is
# "open", so we compare against the wire strings to stay correct even if the
# provider adds a value this SDK version does not know.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
SUCCEEDED = "succeeded"


@dataclass(frozen=True)
class StartResult:
    workflow_run_id: str
    project_id: str


@dataclass(frozen=True)
class JobSnapshot:
    """A point-in-time view of a workflow run or a project export."""

    status: str
    progress_percentage: float
    error_message: str | None = None
    download_url: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_succeeded(self) -> bool:
        return self.status == SUCCEEDED


def _optional_str(value: str | None | UnsetType) -> str | None:
    """Resolve an SDK ``Optional``/``OptionalNullable`` string to plain str|None."""
    if isinstance(value, UnsetType) or value is UNSET:
        return None
    return value


def _format_error(error_model: object | None) -> str | None:
    """Render an ``ApiErrorModel`` (or None) into a human-readable string."""
    if error_model is None:
        return None
    message = getattr(error_model, "message", None) or "unknown error"
    code = _optional_str(getattr(error_model, "code", UNSET))
    return f"{message} ({code})" if code else str(message)


def _translate[T](op_name: str, call: Callable[[], T]) -> T:
    """Run an SDK call, translating every failure into a VideoGenError.

    Order matters: the specific SDK/transport failures first. A decode failure
    (``ValidationError``/``ValueError``) is *not* an API error and means the
    outcome is unknown; a transport error likewise. Both are kept distinct
    from a provider status error so callers can react appropriately.
    """
    try:
        return call()
    except ApiError as exc:
        detail = ""
        try:
            detail = exc.error.text()
        except (ValueError, AttributeError):  # pragma: no cover - defensive
            detail = ""
        logger.warning("VideoGen %s failed: HTTP %s", op_name, exc.status_code)
        raise VideoGenAPIError(exc.status_code, detail) from exc
    except ValidationError as exc:
        raise VideoGenUnreadableError(
            f"{op_name}: could not read VideoGen's response; outcome unknown"
        ) from exc
    except httpx.HTTPError as exc:
        raise VideoGenUnavailableError(
            f"{op_name}: VideoGen is unreachable; outcome unknown"
        ) from exc


class VideoGenService:
    """A thin, typed wrapper over the four VideoGen operations we use."""

    def __init__(self, client: VideogenClient | None = None) -> None:
        # ``client`` is injectable so tests can supply one built over a stub
        # transport; production code uses the shared client.
        self._client = client if client is not None else get_client()

    def start_script_to_video(self, script: str) -> StartResult:
        """Start a script-to-video workflow run for ``script``.

        Requests the cheap shape: stock footage, voiceover only, 16:9.
        """
        body: ScriptToVideoRequestDict = {
            "script": script,
            "visual_style": {"type_": WorkflowVisualStyleType.STOCK},
            "aspect_ratio": {"width": 16, "height": 9},
        }
        response = _translate(
            "script_to_video",
            lambda: self._client.workflows.script_to_video(body=body),
        )
        if not response.workflow_run_id or not response.project_id:
            raise VideoGenUnreadableError(
                "script_to_video returned no workflow_run_id/project_id; outcome unknown"
            )
        return StartResult(
            workflow_run_id=response.workflow_run_id,
            project_id=response.project_id,
        )

    def get_workflow_run(self, workflow_run_id: str) -> JobSnapshot:
        run = _translate(
            "get_workflow_run",
            lambda: self._client.workflows.get_workflow_run(workflow_run_id),
        )
        return JobSnapshot(
            status=str(run.status),
            progress_percentage=float(run.progress_percentage),
            error_message=_format_error(run.error),
        )

    def export_project(self, project_id: str) -> str:
        """Export the project to a single 720p, 16:9 MP4. Returns the export id."""
        body: ExportProjectRequestDict = {"quality": ExportProjectQuality.HIGH}
        response = _translate(
            "export_project",
            lambda: self._client.projects.export_project(project_id, body=body),
        )
        if not response.export_id:
            raise VideoGenUnreadableError(
                "export_project returned no export_id; outcome unknown"
            )
        return response.export_id

    def get_project_export(self, project_id: str, export_id: str) -> JobSnapshot:
        export = _translate(
            "get_project_export",
            lambda: self._client.projects.get_project_export(project_id, export_id),
        )
        return JobSnapshot(
            status=str(export.status),
            progress_percentage=float(export.progress_percentage),
            error_message=_format_error(export.error),
            download_url=export.download_url,
        )
