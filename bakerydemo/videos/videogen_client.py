"""Thin, typed wrapper over the VideoGen Python SDK.

This is the single boundary where VideoGen is spoken to. It:

* builds a sync :class:`videogen.VideogenClient` from Django settings (the
  credentials are read from the environment; no value is ever hard-coded);
* exposes exactly the operations this integration needs; and
* translates every SDK / transport / decode failure into the integration's own
  typed exceptions (:mod:`bakerydemo.videos.exceptions`).

Host is Django under WSGI, so the **sync** client is used. The client owns a
pooled HTTP transport and must be closed; :class:`VideoGenService` is a context
manager so a pipeline run scopes and closes exactly one client.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import httpx
from django.conf import settings
from pydantic import ValidationError
from videogen import VideogenClient
from videogen.core import ApiError, RawError
from videogen.models import (
    ExportProjectResponse,
    ProjectExport,
    StartWorkflowRunResponse,
    WorkflowRun,
)
from videogen.models.enums import ExportProjectQuality, WorkflowVisualStyleType

from .exceptions import (
    VideoGenConfigError,
    VideoGenRequestError,
    VideoGenUnavailableError,
    VideoGenUnreadableError,
)

T = TypeVar("T")

# Per-request HTTP timeout (seconds). Each individual VideoGen call is quick
# (starts/reads); the long waits are our own polling loops, not one HTTP call.
_HTTP_TIMEOUT = 30.0

# 16:9, as a width:height pair (not pixel dimensions).
_ASPECT_RATIO = {"width": 16, "height": 9}


def build_client() -> VideogenClient:
    """Construct a VideoGen client from settings.

    Raises :class:`VideoGenConfigError` when no API key is configured — nothing
    is sent to the provider, so this is a configuration fault, not a rejection.
    ``VIDEOGEN_BASE_URL`` is used verbatim as the base address when set.
    """
    api_key = getattr(settings, "VIDEOGEN_API_KEY", None)
    if not api_key:
        raise VideoGenConfigError(
            "VIDEOGEN_API_KEY is not configured; cannot talk to VideoGen."
        )

    kwargs: dict[str, object] = {"bearer_auth": api_key, "timeout": _HTTP_TIMEOUT}
    base_url = getattr(settings, "VIDEOGEN_BASE_URL", None)
    if base_url:
        kwargs["base_url"] = base_url
    return VideogenClient(**kwargs)  # type: ignore[arg-type]


def _safe_detail(error: ApiError) -> str:
    """Best-effort text of a provider error body, for logging only.

    Every VideoGen operation is "Case B", so ``error.error`` is always a
    ``RawError``; narrow to it before reading the body.
    """
    body = error.error
    if isinstance(body, RawError):
        try:
            return body.text()
        except Exception:  # noqa: BLE001 - logging detail must never mask the error
            return ""
    return str(body) if body is not None else ""


class VideoGenService:
    """Typed façade over the VideoGen operations used by this integration."""

    def __init__(self, client: VideogenClient) -> None:
        self._client = client

    @classmethod
    def from_settings(cls) -> VideoGenService:
        return cls(build_client())

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VideoGenService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- error boundary ----------------------------------------------------
    def _call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Run an SDK call, translating every failure to a typed exception.

        Order matters: a decode failure raises ``ValidationError`` and a
        transport failure raises an ``httpx`` error — neither is an ``ApiError``
        and both bypass the SDK's response modes. (VideoGen uses a plain bearer
        token, so there is no OAuth token-fetch failure to distinguish.)
        """
        try:
            return fn(*args, **kwargs)
        except ApiError as e:
            raise VideoGenRequestError(e.status_code, _safe_detail(e)) from e
        except ValidationError as e:
            raise VideoGenUnreadableError(
                "Unreadable VideoGen response; outcome unknown."
            ) from e
        except httpx.HTTPError as e:
            raise VideoGenUnavailableError(
                "VideoGen provider unreachable; outcome unknown."
            ) from e

    # -- operations --------------------------------------------------------
    def start_script_to_video(self, script: str) -> StartWorkflowRunResponse:
        """Kick off a narrated, stock-footage, 16:9 video from ``script``.

        Cheap shape only: STOCK visuals (no AI imagery), voiceover with no
        avatar/presenter (``actor_entity_id`` omitted), no remix actions (no
        captions/image-to-video/transitions). Returns immediately with a
        workflow run id.
        """
        body = {
            "script": script,
            "visual_style": {"type_": WorkflowVisualStyleType.STOCK},
            "aspect_ratio": _ASPECT_RATIO,
        }
        return self._call(self._client.workflows.script_to_video, body=body)

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        return self._call(self._client.workflows.get_workflow_run, workflow_run_id)

    def export_project(self, project_id: str) -> ExportProjectResponse:
        """Export the built project to a single 720p (HIGH) MP4.

        Exactly one export per project, at the HIGH tier (720p) — never
        ULTRA_HIGH (4K). Watermark/end-screen left at the free-plan default.
        """
        body = {"quality": ExportProjectQuality.HIGH}
        return self._call(self._client.projects.export_project, project_id, body=body)

    def get_project_export(self, project_id: str, export_id: str) -> ProjectExport:
        return self._call(
            self._client.projects.get_project_export, project_id, export_id
        )
