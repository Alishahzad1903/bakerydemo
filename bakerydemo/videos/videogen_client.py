"""
Thin wrapper around VideoGen's Python SDK for the article-video flow.

Every VideoGen interaction goes through this class. It reads its credentials
from the caller (which reads them from Django settings, which reads them from
the environment), never hard-codes them, and translates provider failures into
the typed exceptions in :mod:`bakerydemo.videos.exceptions`.

The shape of what we ask for is fixed by the account's spend policy and is not
configurable per request:

* visuals come from **stock footage** (``visual_style={"type": "STOCK"}``),
  never AI-generated imagery;
* narration is **voice only** — no ``actor_entity_id`` is sent, so no presenter
  or avatar appears on screen;
* the MP4 is exported at **1080p or below** (``FULL_HIGH`` and lower only;
  ``ULTRA_HIGH`` is refused).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from videogen import VideoGen
from videogen.errors import PollCancelledError
from videogen.errors import VideoGenError as SdkVideoGenError

from .exceptions import (
    VideoGenConfigurationError,
    VideoGenProviderError,
    VideoGenTimeoutError,
)

# Vertical resolution tiers, smallest first. FULL_HIGH is Full HD (1080p);
# ULTRA_HIGH is above 1080p and is therefore never allowed by this integration.
EXPORT_QUALITIES_1080P_OR_BELOW = ("STANDARD", "HIGH", "FULL_HIGH")

# Fixed visual style: stock footage and images, never generated imagery.
STOCK_VISUAL_STYLE = {"type": "STOCK"}


def _describe_sdk_error(exc: SdkVideoGenError) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
    return str(exc) or "VideoGen request failed"


class VideoGenClient:
    def __init__(
        self,
        *,
        api_key: Optional[str],
        base_url: Optional[str] = None,
        export_quality: str = "FULL_HIGH",
        aspect_ratio: Optional[dict] = None,
        voice_id: Optional[str] = None,
        poll_interval_ms: int = 3000,
        timeout_ms: int = 3_600_000,
    ) -> None:
        if not api_key:
            raise VideoGenConfigurationError(
                "VIDEOGEN_API_KEY is not set; cannot talk to VideoGen."
            )
        if export_quality not in EXPORT_QUALITIES_1080P_OR_BELOW:
            raise VideoGenConfigurationError(
                f"Export quality {export_quality!r} is not permitted; must be one "
                f"of {EXPORT_QUALITIES_1080P_OR_BELOW} (1080p or below)."
            )

        self._export_quality = export_quality
        self._aspect_ratio = aspect_ratio or {"width": 16, "height": 9}
        self._voice_id = voice_id
        self._poll_interval_ms = poll_interval_ms
        self._timeout_ms = timeout_ms

        # ``base_url`` overrides the default API address verbatim when provided.
        self._client = VideoGen(api_key=api_key, base_url=base_url or None)

    # -- connectivity ---------------------------------------------------------

    def check_connection(self) -> dict:
        """Validate the API key. Raises a typed error if the key is rejected."""
        with self._provider_errors():
            return self._client.account.get_me()

    # -- production steps -----------------------------------------------------

    def start_script_to_video(self, script: str) -> dict:
        """Start a stock-footage, voice-only script-to-video workflow run."""
        with self._provider_errors():
            started = self._client.workflows.script_to_video(
                script=script,
                visual_style=STOCK_VISUAL_STYLE,
                aspect_ratio=self._aspect_ratio,
                voice_id=self._voice_id,
                # No actor_entity_id -> voice-only narration, no on-screen avatar.
            )
        return {
            "workflow_run_id": started["workflow_run_id"],
            "project_id": started["project_id"],
        }

    def wait_for_workflow(
        self, workflow_run_id: str, on_progress: Optional[Callable[[float], None]] = None
    ) -> dict:
        with self._provider_errors():
            return self._client.poll_workflow_run(
                workflow_run_id,
                poll_interval_ms=self._poll_interval_ms,
                timeout_ms=self._timeout_ms,
                on_progress=on_progress,
            )

    def start_export(self, project_id: str) -> str:
        with self._provider_errors():
            response = self._client.projects.export_project(
                project_id=project_id,
                quality=self._export_quality,
            )
        return response["export_id"]

    def wait_for_export(self, project_id: str, export_id: str) -> dict:
        with self._provider_errors():
            return self._client.poll_project_export(
                project_id,
                export_id,
                poll_interval_ms=self._poll_interval_ms,
                timeout_ms=self._timeout_ms,
            )

    def download_export(self, export: dict, output_path: Path) -> None:
        """Download the finished MP4 to ``output_path``.

        Uses the export's signed ``download_url`` directly, falling back to
        hydrating the export file id if that URL is unavailable.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        download_url = export.get("download_url")
        if download_url:
            with self._provider_errors():
                import httpx

                response = httpx.get(download_url, timeout=180.0, follow_redirects=True)
                if response.status_code >= 400:
                    raise VideoGenProviderError(
                        "Failed to download the exported MP4.",
                        status=response.status_code,
                        body=response.text,
                    )
                output_path.write_bytes(response.content)
            return

        export_file_id = export.get("export_file_id")
        if not export_file_id:
            raise VideoGenProviderError(
                "Export completed but carried no download URL or file id."
            )
        with self._provider_errors():
            self._client.download_file(export_file_id, output_path=str(output_path))

    # -- error translation ----------------------------------------------------

    class _ProviderErrorContext:
        def __init__(self, outer: "VideoGenClient"):
            self._outer = outer

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc is None:
                return False
            if isinstance(exc, TimeoutError):
                raise VideoGenTimeoutError(str(exc)) from exc
            if isinstance(exc, PollCancelledError):
                # Cancellation is a control-flow signal, not a provider failure.
                return False
            if isinstance(exc, SdkVideoGenError):
                raise VideoGenProviderError(
                    _describe_sdk_error(exc),
                    status=getattr(exc, "status", None),
                    body=getattr(exc, "body", None),
                    request_id=getattr(exc, "request_id", None),
                ) from exc
            return False

    def _provider_errors(self) -> "VideoGenClient._ProviderErrorContext":
        return self._ProviderErrorContext(self)
