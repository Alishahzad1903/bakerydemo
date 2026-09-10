"""A thin, opinionated wrapper around VideoGen's official Python SDK.

Every interaction with VideoGen goes through this module. It:

* reads credentials and options from Django settings (never hard-coded);
* honours the optional ``VIDEOGEN_BASE_URL`` override verbatim;
* pins the request shape to the cheapest safe form (stock-footage visuals,
  a narrated voice, a single 720p / 16:9 export — no AI imagery, avatars,
  image-to-video, upscales or standalone media generation);
* translates the SDK's errors and terminal "failed" states into this
  package's typed exceptions.

The high-level surface (:class:`VideoGenClient`) exposes exactly the four
provider steps the article-to-video pipeline needs: start a script-to-video
run, wait for it, export it once, and download the finished MP4.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Any

from django.conf import settings
from videogen import VideoGen
from videogen.errors import PollCancelledError
from videogen.errors import VideoGenError as SdkVideoGenError

from .exceptions import (
    VideoGenAPIError,
    VideoGenAuthError,
    VideoGenConfigurationError,
    VideoGenProductionError,
    VideoGenTimeoutError,
)

logger = logging.getLogger("bakerydemo.videos")

_TERMINAL_SUCCESS = "succeeded"
_TERMINAL_FAILURE = frozenset({"failed", "cancelled"})

ProgressCallback = Callable[[float], None]


@dataclass(frozen=True)
class VideoGenConfig:
    """Resolved VideoGen configuration, sourced entirely from Django settings."""

    api_key: str
    base_url: str | None = None
    # Workflow generation quality. Optional; when unset the provider default is
    # used. Stock footage is sourced (not generated), so this adds no per-item
    # cost — omitting it keeps the request minimal and always valid.
    workflow_quality: str | None = None
    # Visual style for the workflow. "STOCK" selects stock footage; we never opt
    # into AI-generated imagery. This is a required field on the workflow.
    visual_style: dict[str, Any] | None = field(
        default_factory=lambda: {"type": "STOCK"}
    )
    # Export quality tier. "STANDARD" keeps the export at a 720p-class rendition
    # and, being below the top tier, can never be a 4K export.
    export_quality: str | None = "STANDARD"
    request_timeout_seconds: float = 60.0
    poll_interval_seconds: float = 3.0
    poll_timeout_seconds: float = 1800.0

    @classmethod
    def from_settings(cls) -> VideoGenConfig:
        api_key = getattr(settings, "VIDEOGEN_API_KEY", None)
        if not api_key:
            raise VideoGenConfigurationError(
                "VideoGen is not configured: set the VIDEOGEN_API_KEY environment "
                "variable."
            )
        return cls(
            api_key=api_key,
            base_url=getattr(settings, "VIDEOGEN_BASE_URL", None) or None,
            workflow_quality=getattr(settings, "VIDEOGEN_WORKFLOW_QUALITY", None)
            or None,
            visual_style=getattr(settings, "VIDEOGEN_VISUAL_STYLE", None) or None,
            export_quality=getattr(settings, "VIDEOGEN_EXPORT_QUALITY", "STANDARD")
            or None,
            request_timeout_seconds=float(
                getattr(settings, "VIDEOGEN_REQUEST_TIMEOUT_SECONDS", 60.0)
            ),
            poll_interval_seconds=float(
                getattr(settings, "VIDEOGEN_POLL_INTERVAL_SECONDS", 3.0)
            ),
            poll_timeout_seconds=float(
                getattr(settings, "VIDEOGEN_POLL_TIMEOUT_SECONDS", 1800.0)
            ),
        )


@dataclass
class StartedRun:
    """Result of starting a script-to-video workflow run."""

    workflow_run_id: str
    project_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StartedExport:
    """Result of starting a project export."""

    export_id: str
    raw: dict[str, Any] = field(default_factory=dict)


class VideoGenClient:
    """High-level, cost-guarded VideoGen operations for one article video."""

    def __init__(self, config: VideoGenConfig | None = None) -> None:
        self.config = config or VideoGenConfig.from_settings()
        self._sdk: VideoGen | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def sdk(self) -> VideoGen:
        if self._sdk is None:
            try:
                self._sdk = VideoGen(
                    api_key=self.config.api_key,
                    base_url=self.config.base_url,
                    timeout=self.config.request_timeout_seconds,
                )
            except ValueError as exc:  # SDK raises ValueError for a missing key
                raise VideoGenConfigurationError(str(exc)) from exc
        return self._sdk

    def close(self) -> None:
        if self._sdk is not None:
            try:
                self._sdk.close()
            finally:
                self._sdk = None

    def __enter__(self) -> VideoGenClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- error translation ---------------------------------------------------

    def _call(self, description: str, func: Callable[[], Any]) -> Any:
        """Run an SDK call, translating its errors into typed exceptions."""
        try:
            return func()
        except SdkVideoGenError as exc:
            status = getattr(exc, "status", None)
            code = None
            body = getattr(exc, "body", None)
            if isinstance(body, dict):
                code = body.get("code") or body.get("type")
            request_id = getattr(exc, "request_id", None)
            message = f"VideoGen {description} failed: {exc}"
            if status in (401, 403):
                raise VideoGenAuthError(
                    message,
                    status=status,
                    code=code,
                    body=body,
                    request_id=request_id,
                ) from exc
            raise VideoGenAPIError(
                message,
                status=status,
                code=code,
                body=body,
                request_id=request_id,
            ) from exc
        except PollCancelledError as exc:  # pragma: no cover - defensive
            raise VideoGenAPIError(
                f"VideoGen {description} was cancelled: {exc}"
            ) from exc
        except (
            VideoGenConfigurationError,
            VideoGenProductionError,
            VideoGenTimeoutError,
        ):
            raise
        except Exception as exc:  # network / unexpected transport errors
            raise VideoGenAPIError(f"VideoGen {description} failed: {exc}") from exc

    # -- operations ----------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        """Return the account behind the key. Handy as a connection test."""
        return self._call("account lookup", lambda: self.sdk.account.get_me())

    def start_script_to_video(self, script: str) -> StartedRun:
        """Start a script-to-video run for ``script`` using the cheap shape.

        Only ``script`` (and, optionally, a low generation quality / explicit
        visual style from settings) is sent. No remix actions, actor/avatar
        fields, or aspect overrides are included: narration is a voice, visuals
        default to stock footage, and the aspect ratio defaults to 16:9.
        """
        body: dict[str, Any] = {"script": script}
        if self.config.workflow_quality:
            body["quality"] = self.config.workflow_quality
        if self.config.visual_style:
            body["visual_style"] = self.config.visual_style

        logger.info("Starting VideoGen script-to-video run (script=%r)", script)
        started = self._call(
            "script-to-video start",
            lambda: self.sdk.workflows.script_to_video(**body),
        )
        started = started or {}
        run_id = started.get("workflow_run_id")
        if not run_id:
            raise VideoGenAPIError(
                "VideoGen did not return a workflow_run_id for the script-to-video "
                "run.",
                body=started,
            )
        return StartedRun(
            workflow_run_id=run_id,
            project_id=started.get("project_id"),
            raw=started,
        )

    def wait_for_run(
        self,
        workflow_run_id: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Poll a workflow run until it succeeds, or raise a typed error."""
        return self._poll(
            kind="workflow run",
            fetch=lambda: self.sdk.workflows.get_workflow_run(
                workflow_run_id=workflow_run_id
            ),
            on_progress=on_progress,
        )

    def start_export(self, project_id: str) -> StartedExport:
        """Start a single MP4 export for ``project_id`` (720p-class, never 4K)."""
        body: dict[str, Any] = {}
        if self.config.export_quality:
            body["quality"] = self.config.export_quality

        logger.info("Starting VideoGen export for project %s", project_id)
        started = self._call(
            "export start",
            lambda: self.sdk.projects.export_project(project_id=project_id, **body),
        )
        started = started or {}
        export_id = started.get("export_id")
        if not export_id:
            raise VideoGenAPIError(
                "VideoGen did not return an export_id for the project export.",
                body=started,
            )
        return StartedExport(export_id=export_id, raw=started)

    def wait_for_export(
        self,
        project_id: str,
        export_id: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Poll a project export until it succeeds, or raise a typed error."""
        return self._poll(
            kind="export",
            fetch=lambda: self.sdk.projects.get_project_export(
                project_id=project_id, export_id=export_id
            ),
            on_progress=on_progress,
        )

    def download_file(self, file_id: str) -> bytes:
        """Download a finished file's bytes (re-hydrating signed URLs as needed)."""
        logger.info("Downloading VideoGen file %s", file_id)
        return self._call("file download", lambda: self.sdk.download_file(file_id))

    # -- internals -----------------------------------------------------------

    def _poll(
        self,
        *,
        kind: str,
        fetch: Callable[[], Any],
        on_progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        deadline = monotonic() + self.config.poll_timeout_seconds
        while True:
            data = self._call(f"{kind} status", fetch) or {}
            status = data.get("status")

            progress = data.get("progress_percentage")
            if on_progress is not None and isinstance(progress, (int, float)):
                on_progress(float(progress))

            if status == _TERMINAL_SUCCESS:
                return data
            if status in _TERMINAL_FAILURE:
                message = _extract_error_message(data) or f"VideoGen {kind} {status}."
                raise VideoGenProductionError(
                    f"VideoGen {kind} {status}: {message}",
                    provider_status=status,
                    body=data,
                )

            if monotonic() >= deadline:
                raise VideoGenTimeoutError(
                    f"Timed out after {self.config.poll_timeout_seconds:.0f}s waiting "
                    f"for VideoGen {kind} to finish (last status={status!r})."
                )
            sleep(self.config.poll_interval_seconds)


def _extract_error_message(data: dict[str, Any]) -> str | None:
    error = data.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return None
