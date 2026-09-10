"""Test doubles for the VideoGen integration.

``StubTransport`` fakes the SDK's transport protocol (the documented test seam),
so gateway tests exercise the *real* request-building and decode pipeline with no
network. ``FakeGateway`` fakes our own gateway, so state-machine and endpoint
tests never construct an SDK client at all.
"""

from __future__ import annotations

import json
from collections import Counter

import httpx
from videogen.core import HttpRequest, HttpResponse, StreamedResponse
from videogen.models import (
    ExportProjectResponse,
    ProjectExport,
    StartWorkflowRunResponse,
    WorkflowRun,
)

# --- Transport-level stub (for gateway/error-boundary tests) -----------------


class StubTransport:
    """Satisfies the SDK's sync transport protocol: send(), stream(), close()."""

    def __init__(self, *responses: HttpResponse) -> None:
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._responses.pop(0)

    def stream(self, request: HttpRequest) -> StreamedResponse:
        raise NotImplementedError("this stub answers send() only")

    def close(self) -> None: ...

    @property
    def last_request(self) -> HttpRequest | None:
        return self.requests[-1] if self.requests else None


class BoomTransport:
    """A transport that fails at the socket, like a real connection error."""

    def send(self, request: HttpRequest) -> HttpResponse:
        raise httpx.ConnectError("connection refused")

    def stream(self, request: HttpRequest) -> StreamedResponse:
        raise httpx.ConnectError("connection refused")

    def close(self) -> None: ...


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


# --- Model builders (wire-shaped, via model_validate) ------------------------


def make_start_response(
    workflow_run_id: str = "vg_work_test", project_id: str = "vg_proj_test"
) -> StartWorkflowRunResponse:
    return StartWorkflowRunResponse.model_validate(
        {
            "workflowRunId": workflow_run_id,
            "projectId": project_id,
            "projectUrl": "https://app.videogen.io/projects/test",
            "remixActionIds": [],
        }
    )


def make_workflow_run(
    status: str, progress: float = 0.0, error_message: str | None = None
) -> WorkflowRun:
    return WorkflowRun.model_validate(
        {
            "workflowRunId": "vg_work_test",
            "status": status,
            "workflowType": "SCRIPT_TO_VIDEO",
            "progressPercentage": progress,
            "attemptIndex": 0,
            "projectId": "vg_proj_test",
            "projectUrl": "https://app.videogen.io/projects/test",
            "error": {"message": error_message} if error_message else None,
        }
    )


def make_export_response(export_id: str = "vg_expo_test") -> ExportProjectResponse:
    return ExportProjectResponse.model_validate({"exportId": export_id})


def make_project_export(
    status: str,
    progress: float = 0.0,
    download_url: str | None = None,
    error_message: str | None = None,
) -> ProjectExport:
    return ProjectExport.model_validate(
        {
            "exportId": "vg_expo_test",
            "projectId": "vg_proj_test",
            "status": status,
            "progressPercentage": progress,
            "attemptIndex": 0,
            "downloadUrl": download_url,
            "downloadUrlExpiresAt": 1_900_000_000 if download_url else None,
            "thumbnailUrl": None,
            "thumbnailUrlExpiresAt": None,
            "exportFileId": "vg_file_test" if download_url else None,
            "file": None,
            "error": {"message": error_message} if error_message else None,
        }
    )


# --- Gateway-level fake (for state-machine / endpoint tests) -----------------


class FakeGateway:
    """A scripted stand-in for ``VideoGenGateway`` (no SDK, no network).

    ``workflow_states`` / ``export_states`` are consumed one per poll; the last
    value repeats once exhausted. ``download_url`` is returned once the export
    reports ``succeeded``.
    """

    def __init__(
        self,
        workflow_states: list[str] | None = None,
        export_states: list[str] | None = None,
        download_url: str = "https://signed.example/video.mp4",
        workflow_error: str | None = None,
        export_error: str | None = None,
    ) -> None:
        self.workflow_states = workflow_states or ["succeeded"]
        self.export_states = export_states or ["succeeded"]
        self.download_url = download_url
        self.workflow_error = workflow_error
        self.export_error = export_error
        self.calls: Counter[str] = Counter()
        self.last_script: str | None = None

    @staticmethod
    def _next(states: list[str]) -> str:
        return states.pop(0) if len(states) > 1 else states[0]

    def start_workflow(self, script: str) -> StartWorkflowRunResponse:
        self.calls["start_workflow"] += 1
        self.last_script = script
        return make_start_response()

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        self.calls["get_workflow_run"] += 1
        state = self._next(self.workflow_states)
        progress = 100.0 if state == "succeeded" else 50.0
        return make_workflow_run(state, progress, self.workflow_error)

    def start_export(self, project_id: str) -> ExportProjectResponse:
        self.calls["start_export"] += 1
        return make_export_response()

    def get_export(self, project_id: str, export_id: str) -> ProjectExport:
        self.calls["get_export"] += 1
        state = self._next(self.export_states)
        url = self.download_url if state == "succeeded" else None
        progress = 100.0 if state == "succeeded" else 50.0
        return make_project_export(state, progress, url, self.export_error)
