"""Test doubles for the VideoGen client and its HTTP transport."""

from __future__ import annotations

from bakerydemo.video.videogen.client import (
    ProjectExport,
    ScriptToVideoResult,
    WorkflowRun,
)


def workflow_run(status, progress=0, project_id="vg_proj_1", error=None):
    return WorkflowRun(
        workflow_run_id="vg_wfr_1",
        status=status,
        progress_percentage=progress,
        project_id=project_id,
        error_message=error,
        raw={},
    )


def project_export(status, progress=0, download_url=None, file_id="vg_file_1", error=None):
    return ProjectExport(
        export_id="vg_exp_1",
        status=status,
        progress_percentage=progress,
        download_url=download_url,
        export_file_id=file_id,
        error_message=error,
        raw={},
    )


class FakeVideoGenClient:
    """A scripted stand-in for :class:`VideoGenClient`.

    ``workflow_runs`` and ``exports`` are sequences of snapshots returned on
    successive polls (the final entry repeats). Call counters let tests assert
    that a video is produced / exported exactly once.
    """

    def __init__(self, *, workflow_runs=None, exports=None, file_bytes=b"FAKE-MP4-BYTES"):
        self._workflow_runs = list(workflow_runs or [workflow_run("succeeded", 100)])
        self._exports = list(
            exports
            or [project_export("succeeded", 100, download_url="https://signed/vid.mp4")]
        )
        self.file_bytes = file_bytes
        self.create_calls = 0
        self.export_calls = 0
        self.download_calls = 0
        self.last_script = None
        self.last_quality = None

    def create_script_to_video(self, script, *, aspect_ratio="16:9"):
        self.create_calls += 1
        self.last_script = script
        self.last_aspect_ratio = aspect_ratio
        return ScriptToVideoResult(workflow_run_id="vg_wfr_1", project_id="vg_proj_1")

    def get_workflow_run(self, workflow_run_id):
        return self._advance(self._workflow_runs)

    def start_export(self, project_id, *, quality=None):
        self.export_calls += 1
        self.last_quality = quality
        self.last_project = project_id
        return "vg_exp_1"

    def get_export(self, project_id, export_id):
        return self._advance(self._exports)

    def download_file(self, url):
        self.download_calls += 1
        self.last_download_url = url
        return self.file_bytes

    @staticmethod
    def _advance(sequence):
        if not sequence:
            raise AssertionError("FakeVideoGenClient: no scripted response left")
        if len(sequence) == 1:
            return sequence[0]
        return sequence.pop(0)


class FakeResponse:
    def __init__(self, status_code, json_body=None, content=b""):
        self.status_code = status_code
        self._json = json_body
        self.content = content if content else (b"{}" if json_body is None else b"x")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Queues responses for ``request``/``get`` and records the calls made."""

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json, headers))
        return self._next()

    def get(self, url, timeout=None, stream=False):
        self.calls.append(("GET", url, None, None))
        return self._next()

    def _next(self):
        if not self._responses:
            raise AssertionError("FakeSession: no queued response")
        return self._responses.pop(0)
