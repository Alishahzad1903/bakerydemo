"""The VideoGen provider boundary.

This module owns the one and only :class:`videogen.VideogenClient` and is the
single place that talks to VideoGen. Every SDK failure kind is translated into a
typed :mod:`bakerydemo.videos.exceptions` error here, so no ``videogen.*``,
``httpx.*`` or ``pydantic.*`` type escapes this layer.

The request shapes are fixed to the cheapest video that satisfies the brief:

* narration = a short script (built elsewhere, from the article's own words);
* visuals = **stock footage only** (never AI-generated imagery);
* narration delivery = a **voice only** (no avatar/presenter — ``actor_entity_id``
  is left unset);
* aspect ratio = **16:9**;
* no remix actions (captions, image-to-video, transitions, logos are all extra
  billed work);
* a single export at **720p** (the ``STANDARD`` tier — see ``EXPORT_QUALITY_720P``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar, cast

import httpx
from django.conf import settings
from pydantic import ValidationError
from videogen import VideogenClient
from videogen.core import ApiError, RawError
from videogen.models import (
    ExportProjectRequestDict,
    ExportProjectResponse,
    ProjectExport,
    ScriptToVideoRequestDict,
    StartWorkflowRunResponse,
    WorkflowRun,
)

from .exceptions import (
    VideoGenAPIError,
    VideoGenConfigError,
    VideoGenResponseError,
    VideoGenTransportError,
)

T = TypeVar("T")

# 16:9, stock footage, single 720p export — the fixed cheap shape.
ASPECT_WIDTH = 16
ASPECT_HEIGHT = 9
VISUAL_STYLE_STOCK = "STOCK"
# 720p == the ``STANDARD`` tier. Empirically confirmed against a real export:
# ``HIGH`` renders 1920x1080 (1080p), so the tier below it, ``STANDARD``, is the
# 720p one, and ``ULTRA_HIGH`` is the 4K tier to avoid. The plugin documents the
# tiers only as a "vertical resolution" ladder, not by pixel size.
EXPORT_QUALITY_720P = "STANDARD"

_client: VideogenClient | None = None
_client_lock = threading.Lock()


def get_client() -> VideogenClient:
    """Return the process-wide, long-lived sync VideoGen client.

    The client owns a pooled (thread-safe) HTTP transport and is built once and
    reused for the app's lifetime — never per request. Credentials are read from
    Django settings (which read the environment); nothing is hard-coded.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                api_key = getattr(settings, "VIDEOGEN_API_KEY", None)
                if not api_key:
                    raise VideoGenConfigError(
                        "VIDEOGEN_API_KEY is not configured; cannot reach VideoGen."
                    )
                kwargs: dict[str, object] = {
                    "bearer_auth": api_key,
                    "timeout": float(getattr(settings, "VIDEOGEN_TIMEOUT", 30.0)),
                }
                # Optional override: used verbatim as the base address when set.
                base_url = getattr(settings, "VIDEOGEN_BASE_URL", None)
                if base_url:
                    kwargs["base_url"] = base_url
                _client = VideogenClient(**kwargs)  # type: ignore[arg-type]
    return _client


def close_client() -> None:
    """Close the shared client's transport (best effort, at process shutdown)."""
    global _client
    if _client is not None:
        with _client_lock:
            if _client is not None:
                _client.close()
                _client = None


class VideoGenService:
    """Thin, error-translating wrapper over the VideoGen operations we use."""

    def __init__(self, client: VideogenClient | None = None) -> None:
        self._client = client or get_client()

    # -- billed operations (each must run exactly once per job) --------------

    def start_script_to_video(self, *, script: str) -> StartWorkflowRunResponse:
        """Start building a video from ``script`` (stock visuals, voice, 16:9)."""
        body: ScriptToVideoRequestDict = {
            "script": script,
            "visual_style": {"type_": VISUAL_STYLE_STOCK},
            "aspect_ratio": {"width": ASPECT_WIDTH, "height": ASPECT_HEIGHT},
        }
        return self._call(lambda: self._client.workflows.script_to_video(body=body))

    def export_project(self, project_id: str) -> ExportProjectResponse:
        """Export ``project_id`` once, at 720p, to a downloadable MP4."""
        body: ExportProjectRequestDict = {"quality": EXPORT_QUALITY_720P}
        return self._call(
            lambda: self._client.projects.export_project(project_id, body=body)
        )

    # -- read operations (safe to poll) --------------------------------------

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        return self._call(
            lambda: self._client.workflows.get_workflow_run(workflow_run_id)
        )

    def get_project_export(self, project_id: str, export_id: str) -> ProjectExport:
        return self._call(
            lambda: self._client.projects.get_project_export(project_id, export_id)
        )

    # -- error translation ---------------------------------------------------

    @staticmethod
    def _call(fn: Callable[[], T]) -> T:
        """Run ``fn`` and translate every SDK failure kind to a typed error.

        Order matters and follows the SDK's own failure model:

        * ``ApiError`` — the provider returned a non-2xx (``.error`` is a
          ``RawError`` in this SDK). Preserve the status; pull ``code``/``message``
          from the standard error body when present.
        * ``ValidationError``/``ValueError`` — a decode failure. These are *not*
          API errors and bypass both response modes; the outcome is unknown.
        * ``httpx.HTTPError`` — a transport failure, surfaced unwrapped by the SDK.
        """
        try:
            return fn()
        except ApiError as exc:
            code, message = _extract_error_details(exc)
            raise VideoGenAPIError(exc.status_code, message, code) from exc
        except ValidationError as exc:
            raise VideoGenResponseError(
                "VideoGen returned an undecodable response; outcome unknown."
            ) from exc
        except ValueError as exc:
            # A non-JSON body raises a plain ValueError during decoding.
            raise VideoGenResponseError(
                "VideoGen returned an unreadable response; outcome unknown."
            ) from exc
        except httpx.HTTPError as exc:
            raise VideoGenTransportError("VideoGen is unreachable.") from exc


def _extract_error_details(exc: ApiError) -> tuple[str | None, str]:
    """Pull ``(code, message)`` from a VideoGen error body without leaking it raw."""
    # Every operation in this SDK is Case B, so ``exc.error`` is always a RawError.
    raw = cast(RawError, exc.error)
    code: str | None = None
    message = ""
    try:
        body = raw.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        raw_code = body.get("code")
        code = raw_code if isinstance(raw_code, str) else None
        raw_message = body.get("message")
        if isinstance(raw_message, str):
            message = raw_message
    if not message:
        try:
            message = raw.text() or f"HTTP {exc.status_code}"
        except (UnicodeError, ValueError):  # pragma: no cover - defensive
            message = f"HTTP {exc.status_code}"
    return code, message
