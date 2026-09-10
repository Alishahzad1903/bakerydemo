"""VideoGen SDK boundary.

Owns the single long-lived :class:`~videogen.VideogenClient` and wraps every SDK call
so that provider failures are re-raised as the typed exceptions in
:mod:`bakerydemo.videos.exceptions`. Nothing outside this module imports the SDK, so the
translation is guaranteed to be the only path to a VideoGen call.

Only the *cheap shape* the task mandates is ever requested:
  * script-to-video with **stock footage only** (never AI imagery),
  * **voiceover only** (no actor/avatar/presenter),
  * **16:9**,
  * a **single 720p export** per project.
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from django.conf import settings
from pydantic import ValidationError
from videogen import VideogenClient
from videogen.core import UNSET, ApiError
from videogen.models import (
    ApiErrorModel,
    ExportProjectRequestDict,
    ExportProjectResponse,
    ProjectExport,
    ScriptToVideoRequestDict,
    StartWorkflowRunResponse,
    WorkflowRun,
)
from videogen.models.enums import ExportProjectQuality, WorkflowVisualStyleType

from .exceptions import (
    VideoGenAPIError,
    VideoGenConfigurationError,
    VideoGenProtocolError,
    VideoGenUnavailableError,
)

# 16:9, per the task. Constants rather than settings: these are fixed by the spend policy.
_ASPECT_WIDTH = 16
_ASPECT_HEIGHT = 9

# Export tiers we refuse outright to protect against an accidental (expensive) 4K render.
_FORBIDDEN_EXPORT_QUALITIES = frozenset({ExportProjectQuality.ULTRA_HIGH.value})

_client: VideogenClient | None = None
_client_lock = threading.Lock()


def get_client() -> VideogenClient:
    """Return the process-wide VideoGen client, building it once on first use.

    The client owns an ``httpx`` connection pool and is meant to be long-lived, so it is
    memoised at module scope and closed at interpreter shutdown. Raises
    :class:`VideoGenConfigurationError` when no API key is configured (otherwise the SDK
    would silently send unauthenticated requests).
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        api_key = getattr(settings, "VIDEOGEN_API_KEY", None)
        if not api_key:
            raise VideoGenConfigurationError(
                "VIDEOGEN_API_KEY is not set; cannot talk to VideoGen."
            )
        # Empty/unset override => SDK default host; a real value is used verbatim.
        base_url = getattr(settings, "VIDEOGEN_BASE_URL", None) or None
        timeout = float(getattr(settings, "VIDEOGEN_HTTP_TIMEOUT", 60.0))
        client = VideogenClient(
            bearer_auth=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        atexit.register(client.close)
        _client = client
        return _client


@contextmanager
def _translate_provider_errors() -> Iterator[None]:
    """Translate SDK/transport failures into typed :class:`VideoGenError` subclasses.

    Order matters (most specific first): a non-2xx ``ApiError`` becomes a
    :class:`VideoGenAPIError`; a decode failure (``ValidationError``/``ValueError`` — these
    bypass the SDK's error handling) becomes a :class:`VideoGenProtocolError`; an ``httpx``
    transport error (which the SDK does not wrap) becomes a :class:`VideoGenUnavailableError`.
    """
    try:
        yield
    except ApiError as exc:
        code: str | None = None
        message: str | None = None
        raw = exc.error  # Case B for every op in scope: a RawError.
        try:
            body = raw.json()
        except Exception:  # noqa: BLE001 - json() raises ValueError on a non-JSON body
            body = None
        if isinstance(body, dict):
            raw_message = body.get("message")
            raw_code = body.get("code")
            message = raw_message if isinstance(raw_message, str) else None
            code = raw_code if isinstance(raw_code, str) else None
        if not message:
            try:
                message = raw.text()[:500]
            except Exception:  # noqa: BLE001 - never let logging detail mask the failure
                message = str(exc)
        raise VideoGenAPIError(exc.status_code, message or "VideoGen error", code) from exc
    except ValidationError as exc:
        raise VideoGenProtocolError(
            "VideoGen returned an unreadable response; outcome unknown."
        ) from exc
    except ValueError as exc:
        raise VideoGenProtocolError(
            "VideoGen returned a non-JSON response; outcome unknown."
        ) from exc
    except httpx.HTTPError as exc:
        raise VideoGenUnavailableError(
            "VideoGen is unreachable; outcome unknown."
        ) from exc


def start_script_to_video(script: str) -> StartWorkflowRunResponse:
    """Start a script-to-video workflow with the mandated cheap shape.

    Stock footage only, voiceover only (no avatar), 16:9. The narration ``script`` is used
    verbatim by VideoGen (never rewritten or expanded).
    """
    client = get_client()
    # Built as the SDK's TypedDict companions (keyed by Python names) so the call
    # type-checks cleanly under plain mypy despite the wire aliases.
    body: ScriptToVideoRequestDict = {
        "script": script,
        "visual_style": {"type_": WorkflowVisualStyleType.STOCK},
        "aspect_ratio": {"width": _ASPECT_WIDTH, "height": _ASPECT_HEIGHT},
    }
    with _translate_provider_errors():
        resp = client.workflows.script_to_video(body=body)
    if not resp.workflow_run_id or not resp.project_id:
        raise VideoGenProtocolError(
            "script_to_video returned no workflow/project id; outcome unknown."
        )
    return resp


def get_workflow_run(workflow_run_id: str) -> WorkflowRun:
    client = get_client()
    with _translate_provider_errors():
        run = client.workflows.get_workflow_run(workflow_run_id)
    if not run.status:
        raise VideoGenProtocolError("get_workflow_run returned no status.")
    return run


def start_export(project_id: str, quality: str) -> ExportProjectResponse:
    """Start a single MP4 export of the project at the configured tier.

    Refuses 4K (``ULTRA_HIGH``) outright as a spend safeguard.
    """
    normalized = str(quality).upper()
    if normalized in _FORBIDDEN_EXPORT_QUALITIES:
        raise VideoGenConfigurationError(
            f"Export quality {normalized!r} is forbidden by spend policy (no 4K)."
        )
    client = get_client()
    body: ExportProjectRequestDict = {"quality": normalized}
    with _translate_provider_errors():
        resp = client.projects.export_project(project_id, body=body)
    if not resp.export_id:
        raise VideoGenProtocolError(
            "export_project returned no export id; outcome unknown."
        )
    return resp


def get_project_export(project_id: str, export_id: str) -> ProjectExport:
    client = get_client()
    with _translate_provider_errors():
        export = client.projects.get_project_export(project_id, export_id)
    if not export.status:
        raise VideoGenProtocolError("get_project_export returned no status.")
    return export


def download_bytes(url: str) -> bytes:
    """Fetch the finished MP4 from its signed URL (a plain HTTP GET, not a VideoGen API
    call — no auth, not billed). Transport failures become :class:`VideoGenUnavailableError`."""
    try:
        with httpx.Client(timeout=120.0, follow_redirects=True) as http:
            resp = http.get(url)
            resp.raise_for_status()
            return resp.content
    except httpx.HTTPError as exc:
        raise VideoGenUnavailableError("Could not download the exported MP4.") from exc


def error_model_to_pair(error: ApiErrorModel | None, default_code: str) -> tuple[str, str]:
    """Normalise a VideoGen ``ApiErrorModel`` (or ``None``) into ``(code, message)``."""
    if error is None:
        return default_code, "VideoGen reported a failure."
    message = getattr(error, "message", None) or "VideoGen reported a failure."
    code = getattr(error, "code", None)
    if code is UNSET or not isinstance(code, str) or not code:
        code = default_code
    return code, message
