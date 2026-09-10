"""A scriptable fake VideoGen client for tests.

It records the mutating calls it receives (so tests can assert a video/export is
started exactly once) and returns caller-supplied status payloads for polls.
"""

from __future__ import annotations

from bakerydemo.videogen.exceptions import VideoGenError


class FakeVideoGenClient:
    def __init__(
        self,
        *,
        run_states=None,
        export_states=None,
        create_error: Exception | None = None,
        export_error: Exception | None = None,
        download_error: Exception | None = None,
        download_bytes: bytes = b"FAKE-MP4-BYTES",
    ):
        self.run_states = list(run_states or [])
        self.export_states = list(export_states or [])
        self.create_error = create_error
        self.export_error = export_error
        self.download_error = download_error
        self.download_bytes = download_bytes
        self.create_calls = []
        self.export_calls = []
        self.download_calls = []

    def script_to_video(self, *, script, voice_id=None):
        self.create_calls.append(script)
        if self.create_error:
            raise self.create_error
        return {"workflowRunId": "wr_test", "projectId": "pr_test"}

    def get_workflow_run(self, workflow_run_id):
        return self.run_states.pop(0)

    def export_project(self, project_id, *, quality="720p"):
        self.export_calls.append((project_id, quality))
        if self.export_error:
            raise self.export_error
        return {"exportId": "ex_test"}

    def get_export(self, project_id, export_id):
        return self.export_states.pop(0)

    def download_file(self, url):
        self.download_calls.append(url)
        if self.download_error:
            raise self.download_error
        return self.download_bytes


def running(pct=10):
    return {"status": "running", "progressPercentage": pct}


def succeeded(**extra):
    return {"status": "succeeded", "progressPercentage": 100, **extra}


def failed(message="boom"):
    return {"status": "failed", "error": {"message": message}}


class BoomError(VideoGenError):
    pass
