"""A configurable in-memory stand-in for :class:`VideoGenClient`.

Used throughout the test suite so that not a single test touches the real
VideoGen API (which is billed). It records every call it receives so tests can
assert the *cost-safety* contract: exactly one workflow, exactly one export.
"""

from __future__ import annotations

from bakerydemo.video.exceptions import (
    VideoGenBadRequestError,
    VideoGenJobFailedError,
)


class FakeVideoGenClient:
    def __init__(
        self,
        *,
        workflow_polls_running=0,
        export_polls_running=0,
        fail_workflow=False,
        fail_export=False,
        raise_on_create=None,
        succeed_without_download_url=False,
    ):
        self.workflow_polls_running = workflow_polls_running
        self.export_polls_running = export_polls_running
        self.fail_workflow = fail_workflow
        self.fail_export = fail_export
        self.raise_on_create = raise_on_create
        self.succeed_without_download_url = succeed_without_download_url

        self.calls: list[str] = []
        self.script = None
        self.export_quality = None
        self.create_kwargs = None
        self._wf_poll_count = 0
        self._exp_poll_count = 0

    # -- workflow ----------------------------------------------------------

    def create_script_to_video(
        self,
        *,
        script,
        aspect_ratio="16:9",
        visual_style_type="STOCK",
        voice_id=None,
    ):
        self.calls.append("create")
        self.script = script
        self.create_kwargs = {
            "aspect_ratio": aspect_ratio,
            "visual_style_type": visual_style_type,
            "voice_id": voice_id,
        }
        if self.raise_on_create is not None:
            raise self.raise_on_create
        return {"workflowRunId": "wf_test", "projectId": "proj_test"}

    def get_workflow_run(self, workflow_run_id):
        self.calls.append("wf_poll")
        self._wf_poll_count += 1
        if self._wf_poll_count <= self.workflow_polls_running:
            return {"status": "running", "progressPercentage": 40}
        if self.fail_workflow:
            return {
                "status": "failed",
                "progressPercentage": 40,
                "error": {"message": "stock footage lookup failed"},
            }
        return {
            "status": "succeeded",
            "progressPercentage": 100,
            "projectId": "proj_test",
        }

    # -- export ------------------------------------------------------------

    def export_project(self, project_id, *, quality="HD"):
        self.calls.append("export")
        self.export_quality = quality
        return {"exportId": "exp_test"}

    def get_project_export(self, project_id, export_id):
        self.calls.append("exp_poll")
        self._exp_poll_count += 1
        if self._exp_poll_count <= self.export_polls_running:
            return {"status": "running", "progressPercentage": 60}
        if self.fail_export:
            return {
                "status": "failed",
                "progressPercentage": 60,
                "error": "render error",
            }
        payload = {
            "status": "succeeded",
            "progressPercentage": 100,
            "exportFileId": "file_test",
        }
        if not self.succeed_without_download_url:
            payload["downloadUrl"] = "https://signed.example/video.mp4"
        return payload

    # -- download ----------------------------------------------------------

    def download_file(self, download_url):
        self.calls.append("download")
        return b"FAKE-MP4-BYTES"

    # -- assertions helpers ------------------------------------------------

    @property
    def create_count(self):
        return self.calls.count("create")

    @property
    def export_count(self):
        return self.calls.count("export")


__all__ = [
    "FakeVideoGenClient",
    "VideoGenBadRequestError",
    "VideoGenJobFailedError",
]
