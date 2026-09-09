"""A fake transport for the VideoGen SDK, for tests that must not hit the network.

The SDK takes a transport in its constructor (``custom_http_client=``); passing
one of these exercises the real request-building/decoding pipeline with no real
calls. VideoGen uses a plain bearer token (no lazy OAuth token fetch), so the
first request the stub sees is the operation itself — no token request to queue.
"""

from __future__ import annotations

import json

from videogen import VideogenClient
from videogen.core import HttpRequest, HttpResponse


class StubTransport:
    """Satisfies the SDK's sync transport protocol: send/stream/close."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("StubTransport ran out of queued responses")
        return self._responses.pop(0)

    def stream(self, request: HttpRequest):
        raise NotImplementedError("this stub answers send() only")

    def close(self) -> None:
        pass

    @property
    def last_request(self) -> HttpRequest | None:
        return self.requests[-1] if self.requests else None


class RaisingTransport:
    """A transport whose send() raises, to simulate a transport failure."""

    def __init__(self, exc):
        self._exc = exc
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        raise self._exc

    def stream(self, request: HttpRequest):
        raise self._exc

    def close(self) -> None:
        pass


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def client_with(transport) -> VideogenClient:
    return VideogenClient(custom_http_client=transport, bearer_auth="test-token")


# --- Ready-made response bodies (wire shapes) -------------------------------


def start_workflow_body(workflow_run_id="vg_work_1", project_id="vg_proj_1"):
    return {
        "workflowRunId": workflow_run_id,
        "projectId": project_id,
        "projectUrl": "https://app.videogen.io/projects/vg_proj_1",
        "remixActionIds": [],
    }


def workflow_run_body(status, progress=0.0, error=None):
    return {
        "workflowRunId": "vg_work_1",
        "status": status,
        "workflowType": "SCRIPT_TO_VIDEO",
        "progressPercentage": progress,
        "attemptIndex": 0,
        "projectId": "vg_proj_1",
        "projectUrl": "https://app.videogen.io/projects/vg_proj_1",
        "error": error,
    }


def export_started_body(export_id="vg_expo_1"):
    return {"exportId": export_id}


def project_export_body(status, progress=0.0, download_url=None, error=None):
    return {
        "exportId": "vg_expo_1",
        "projectId": "vg_proj_1",
        "status": status,
        "progressPercentage": progress,
        "attemptIndex": 0,
        "downloadUrl": download_url,
        "downloadUrlExpiresAt": None,
        "thumbnailUrl": None,
        "thumbnailUrlExpiresAt": None,
        "exportFileId": "vg_file_1" if download_url else None,
        "file": None,
        "error": error,
    }
