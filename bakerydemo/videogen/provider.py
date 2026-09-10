"""Thin, opinionated wrapper around the VideoGen API.

This is the *only* module that talks to VideoGen. It is built on VideoGen's
official Python SDK (``videogen`` on PyPI) and enforces the cost-conscious
shape we always want for turning an article into a shareable clip:

* **script-to-video** with the narration used verbatim,
* **stock footage** visuals (``visualStyle.type = "STOCK"``) — we never opt
  into AI-generated imagery (that would require ``type = "AI_IMAGE"``),
* **voice-only** narration — no presenter, avatar or talking head,
* a **single** MP4 **export** at the project's default (non-4K) rendition,
* **16:9** — the workflow default; we never resize afterwards.

Every provider failure is surfaced as a typed
:class:`~bakerydemo.videogen.exceptions.VideoGenIntegrationError` subclass so
callers never have to know about the underlying SDK.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from django.conf import settings
from videogen import (
    VideoGen,
    VideoGenError,
    poll_project_export,
    poll_workflow_run,
)

from .exceptions import (
    VideoGenAPIError,
    VideoGenConfigurationError,
    VideoGenProductionError,
    VideoGenTimeoutError,
)

ProgressCallback = Callable[[float], None]

# How often to poll VideoGen for run/export progress.
_POLL_INTERVAL_MS = 3000

# ``visualStyle`` is a required field on script-to-video. ``"STOCK"`` selects
# real stock footage; the only other value, ``"AI_IMAGE"``, generates imagery
# with AI and is deliberately never used here.
_STOCK_VISUAL_STYLE = {"type": "STOCK"}


@dataclass(frozen=True)
class StartedRun:
    workflow_run_id: str
    project_id: str


@dataclass(frozen=True)
class FinishedExport:
    export_id: str
    download_url: str | None
    export_file_id: str | None


@contextlib.contextmanager
def _translate_errors():
    """Convert SDK/transport errors into typed integration errors."""
    try:
        yield
    except VideoGenError as exc:  # HTTP / API error from VideoGen
        raise VideoGenAPIError(
            str(exc),
            status=getattr(exc, "status", None),
            body=getattr(exc, "body", None),
            request_id=getattr(exc, "request_id", None),
        ) from exc
    except TimeoutError as exc:
        raise VideoGenTimeoutError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise VideoGenAPIError(f"Network error talking to VideoGen: {exc}") from exc


def _terminal_error_message(result: dict, default: str) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        return error.get("message") or default
    if isinstance(error, str) and error:
        return error
    return default


class VideoGenProvider:
    """High-level VideoGen operations used by the article-video service."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        api_key = api_key if api_key is not None else getattr(settings, "VIDEOGEN_API_KEY", "")
        if not api_key:
            raise VideoGenConfigurationError(
                "VIDEOGEN_API_KEY is not set; cannot contact the video provider."
            )
        # ``base_url`` override is used verbatim when provided.
        if base_url is None:
            base_url = getattr(settings, "VIDEOGEN_BASE_URL", None)
        if timeout_ms is None:
            timeout_ms = getattr(settings, "VIDEOGEN_JOB_TIMEOUT_MS", 30 * 60 * 1000)

        self._timeout_ms = timeout_ms
        self._client = VideoGen(api_key=api_key, base_url=base_url or None)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()

    def __enter__(self) -> VideoGenProvider:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -- connection test -----------------------------------------------------

    def check_connection(self) -> dict:
        """Return the account behind the API key. Cheap, does not bill."""
        with _translate_errors():
            return self._client.account.get_me()

    # -- production ----------------------------------------------------------

    def start_script_to_video(self, script: str) -> StartedRun:
        """Kick off a script-to-video workflow run (used verbatim).

        The request body is intentionally minimal: the ``script`` plus
        ``visualStyle = {"type": "STOCK"}`` (a required field). ``STOCK`` selects
        real stock footage rather than AI-generated imagery; we never send
        ``remixActions`` (which would add per-item, billable steps), and the
        workflow defaults to voice-only narration at 16:9.
        """
        script = (script or "").strip()
        if not script:
            raise VideoGenProductionError("Refusing to produce a video for an empty script.")
        with _translate_errors():
            started = self._client.workflows.script_to_video(
                script=script,
                visual_style=_STOCK_VISUAL_STYLE,
            )
        run_id = started.get("workflow_run_id")
        project_id = started.get("project_id")
        if not run_id or not project_id:
            raise VideoGenProductionError(
                "VideoGen did not return a workflow run id / project id."
            )
        return StartedRun(workflow_run_id=run_id, project_id=project_id)

    def wait_for_run(
        self,
        run_id: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> dict:
        """Poll a workflow run until it reaches a terminal state."""
        with _translate_errors():
            run = poll_workflow_run(
                self._client,
                run_id,
                poll_interval_ms=_POLL_INTERVAL_MS,
                timeout_ms=self._timeout_ms,
                throw_on_failure=False,
                on_progress=on_progress,
            )
        status = run.get("status")
        if status != "succeeded":
            raise VideoGenProductionError(
                _terminal_error_message(
                    run, f"Video generation {status or 'did not succeed'}."
                )
            )
        return run

    def export_video(self, project_id: str) -> FinishedExport:
        """Export the finished project **once** and wait for the MP4.

        No resolution/quality override is sent, so VideoGen uses the project's
        default rendition (observed: 1080p, 16:9 — never 4K). The API skill does
        not document an export-resolution parameter, and issuing a second export
        to try a different rendition is explicitly forbidden, so we take the
        single default export rather than guess at an undocumented knob. This
        honours the hard cost constraints (export once, never 4K, 16:9); the
        dominant cost driver — clip length — is bounded by the short script.
        """
        with _translate_errors():
            started = self._client.projects.export_project(project_id=project_id)
            export_id = started.get("export_id")
            if not export_id:
                raise VideoGenProductionError("VideoGen did not return an export id.")
            export = poll_project_export(
                self._client,
                project_id,
                export_id,
                poll_interval_ms=_POLL_INTERVAL_MS,
                timeout_ms=self._timeout_ms,
                throw_on_failure=False,
            )
        status = export.get("status")
        if status != "succeeded":
            raise VideoGenProductionError(
                _terminal_error_message(export, f"Video export {status or 'did not succeed'}.")
            )
        download_url = export.get("download_url")
        export_file_id = export.get("export_file_id")
        if not download_url and export_file_id:
            download_url = self._hydrated_download_url(export_file_id)
        if not download_url:
            raise VideoGenProductionError(
                "Export succeeded but no downloadable MP4 URL was returned."
            )
        return FinishedExport(
            export_id=export_id,
            download_url=download_url,
            export_file_id=export_file_id,
        )

    def _hydrated_download_url(self, file_id: str) -> str | None:
        with _translate_errors():
            info = self._client.files.hydrate_file(file_id=file_id)
        source = (info or {}).get("download_source") or {}
        return source.get("url")

    def download_to(self, download_url: str, fileobj) -> int:
        """Stream the MP4 at ``download_url`` into ``fileobj``. Returns bytes."""
        total = 0
        with _translate_errors():
            with httpx.stream("GET", download_url, timeout=120.0, follow_redirects=True) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                    fileobj.write(chunk)
                    total += len(chunk)
        if total == 0:
            raise VideoGenProductionError("Downloaded MP4 was empty.")
        return total
