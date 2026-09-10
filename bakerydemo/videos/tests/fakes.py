"""In-memory fakes for exercising the pipeline without calling VideoGen."""

from __future__ import annotations

from bakerydemo.videos.videogen.client import ProjectExport, WorkflowRun


class FakeVideoGenClient:
    """A scriptable stand-in for :class:`VideoGenClient`.

    It records the calls it receives and returns queued responses, so tests can
    drive the full success path (or inject failures) with no network and no
    billing.
    """

    def __init__(
        self,
        *,
        workflow_runs=None,
        exports=None,
        create_response=None,
        export_response=None,
        download_bytes=b"\x00\x00\x00\x18ftypmp42FAKEMP4BYTES",
    ):
        self.create_response = create_response or {
            "workflowRunId": "vg_work_fake",
            "projectId": "vg_proj_fake",
        }
        self.export_response = export_response or {"exportId": "vg_export_fake"}
        self._workflow_runs = list(
            workflow_runs
            or [
                WorkflowRun("vg_work_fake", "succeeded", 100, project_id="vg_proj_fake"),
            ]
        )
        self._exports = list(
            exports
            or [
                ProjectExport(
                    "vg_export_fake",
                    "vg_proj_fake",
                    "succeeded",
                    100,
                    download_url="https://files.example/fake.mp4",
                ),
            ]
        )
        self._download_bytes = download_bytes
        self.calls: list[tuple] = []

    def create_script_to_video(self, **kwargs):
        self.calls.append(("create_script_to_video", kwargs))
        return self.create_response

    def get_workflow_run(self, workflow_run_id):
        self.calls.append(("get_workflow_run", workflow_run_id))
        if len(self._workflow_runs) > 1:
            return self._workflow_runs.pop(0)
        return self._workflow_runs[0]

    def export_project(self, project_id, **kwargs):
        self.calls.append(("export_project", project_id, kwargs))
        return self.export_response

    def get_export(self, project_id, export_id):
        self.calls.append(("get_export", project_id, export_id))
        if len(self._exports) > 1:
            return self._exports.pop(0)
        return self._exports[0]

    def stream_download(self, url, chunk_size=None):
        self.calls.append(("stream_download", url))
        yield self._download_bytes

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)
