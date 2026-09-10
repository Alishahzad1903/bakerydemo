"""Test doubles for the VideoGen integration.

``StubTransport`` satisfies the SDK's sync transport protocol so a real
:class:`~videogen.VideogenClient` can be exercised end to end with no network — the
recommended seam for these SDKs. ``FakeVideoGen`` stands in for our own
``videogen_client`` wrapper module when unit-testing the orchestration state machine.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from videogen.core import HttpRequest, HttpResponse, StreamedResponse


class StubTransport:
    """Sync transport protocol: ``send`` / ``stream`` / ``close``, returning queued responses."""

    def __init__(self, *responses: HttpResponse) -> None:
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._responses.pop(0)

    def stream(self, request: HttpRequest) -> StreamedResponse:
        raise NotImplementedError("this stub answers send() only")

    def close(self) -> None:
        pass

    @property
    def last_request(self) -> HttpRequest | None:
        return self.requests[-1] if self.requests else None


class BoomTransport:
    """Transport that always raises a transport error, to model an unreachable provider."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        raise self._exc

    def stream(self, request: HttpRequest) -> StreamedResponse:
        raise self._exc

    def close(self) -> None:
        pass


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def text_response(status: int, text: str, content_type: str = "text/html") -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": content_type},
        content=text.encode(),
    )


def stub_client(*responses: HttpResponse):
    """Build a real VideogenClient wired to a StubTransport. Returns (client, transport)."""
    from videogen import VideogenClient

    transport = StubTransport(*responses)
    client = VideogenClient(bearer_auth="test-token", custom_http_client=transport)
    return client, transport


class FakeVideoGen:
    """Programmable stand-in for the ``videogen_client`` wrapper module.

    Drives the orchestration state machine deterministically. Each queue holds the
    successive results (or exceptions) returned by the corresponding wrapper.
    """

    def __init__(self):
        self.script_to_video_result = SimpleNamespace(
            workflow_run_id="wr_test", project_id="pr_test"
        )
        self.workflow_runs: list = []
        self.export_result = SimpleNamespace(export_id="ex_test")
        self.project_exports: list = []
        self.download_result = b"FAKE_MP4_BYTES"
        self.calls = {
            "start_script_to_video": 0,
            "get_workflow_run": 0,
            "start_export": 0,
            "get_project_export": 0,
            "download_bytes": 0,
        }
        # error_model_to_pair is a pure helper; reuse the real one.
        from bakerydemo.videos.videogen_client import error_model_to_pair

        self.error_model_to_pair = error_model_to_pair

    @staticmethod
    def _next(queue):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def start_script_to_video(self, script):
        self.calls["start_script_to_video"] += 1
        if isinstance(self.script_to_video_result, Exception):
            raise self.script_to_video_result
        return self.script_to_video_result

    def get_workflow_run(self, workflow_run_id):
        self.calls["get_workflow_run"] += 1
        return self._next(self.workflow_runs)

    def start_export(self, project_id, quality):
        self.calls["start_export"] += 1
        if isinstance(self.export_result, Exception):
            raise self.export_result
        return self.export_result

    def get_project_export(self, project_id, export_id):
        self.calls["get_project_export"] += 1
        return self._next(self.project_exports)

    def download_bytes(self, url):
        self.calls["download_bytes"] += 1
        if isinstance(self.download_result, Exception):
            raise self.download_result
        return self.download_result


def workflow_run(status, progress=0.0, error=None):
    return SimpleNamespace(status=status, progress_percentage=progress, error=error)


def project_export(status, progress=0.0, download_url=None, error=None):
    return SimpleNamespace(
        status=status,
        progress_percentage=progress,
        download_url=download_url,
        error=error,
    )
