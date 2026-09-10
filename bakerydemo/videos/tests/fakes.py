"""A scripted in-memory stand-in for :class:`VideoGenClient`.

Used by the service/API tests so the flow can be exercised end to end without
touching the real, billed provider. It records every call, which lets tests
assert the spend guarantees directly: exactly one workflow create and at most
one export per job.
"""

from __future__ import annotations

from ..videogen.exceptions import VideoGenError


class FakeVideoGenClient:
    """Deterministic fake with configurable terminal states.

    Args:
        run_status: terminal status the workflow run resolves to.
        export_status: terminal status the export resolves to.
        run_error / export_error: ``error`` payloads for failed states.
        download_url: URL returned once the export succeeds.
        create_error: if set, raised from :meth:`create_script_to_video`.
    """

    def __init__(
        self,
        *,
        run_status="succeeded",
        export_status="succeeded",
        run_error=None,
        export_error=None,
        download_url="https://videogen.example/download/final.mp4",
        create_error: VideoGenError | None = None,
    ):
        self.run_status = run_status
        self.export_status = export_status
        self.run_error = run_error
        self.export_error = export_error
        self.download_url = download_url
        self.create_error = create_error

        self.create_calls = []
        self.export_calls = []
        self.run_polls = 0
        self.export_polls = 0

    def create_script_to_video(self, *, script, visual_style, aspect_ratio):
        if self.create_error is not None:
            raise self.create_error
        self.create_calls.append(
            {
                "script": script,
                "visual_style": visual_style,
                "aspect_ratio": aspect_ratio,
            }
        )
        return {"workflowRunId": "run_123", "projectId": "proj_123"}

    def get_workflow_run(self, workflow_run_id):
        self.run_polls += 1
        return {
            "workflowRunId": workflow_run_id,
            "status": self.run_status,
            "progressPercentage": 100 if self.run_status == "succeeded" else 40,
            "projectId": "proj_123",
            "error": self.run_error,
        }

    def export_project(self, project_id, *, quality):
        self.export_calls.append({"project_id": project_id, "quality": quality})
        return {"exportId": "exp_123"}

    def get_project_export(self, project_id, export_id):
        self.export_polls += 1
        succeeded = self.export_status == "succeeded"
        return {
            "status": self.export_status,
            "progressPercentage": 100 if succeeded else 50,
            "downloadUrl": self.download_url if succeeded else None,
            "error": self.export_error,
        }
