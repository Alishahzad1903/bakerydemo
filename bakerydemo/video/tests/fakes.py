"""Test doubles for VideoGen so tests never make a real, billed call."""


class FakeVideoGenClient:
    """A stand-in for :class:`~bakerydemo.video.client.VideoGenClient`."""

    def __init__(self, *, download_url="https://cdn.videogen.test/out.mp4"):
        self.download_url = download_url
        self.script_calls = []
        self.export_calls = []
        self.workflow_poll_count = 0
        self.export_poll_count = 0

    def create_script_to_video(self, **kwargs):
        self.script_calls.append(kwargs)
        return {"workflowRunId": "vg_work_test", "projectId": "vg_proj_test"}

    def get_workflow_run(self, workflow_run_id):
        self.workflow_poll_count += 1
        return {
            "status": "succeeded",
            "progressPercentage": 100,
            "projectId": "vg_proj_test",
        }

    def create_export(self, project_id, **kwargs):
        self.export_calls.append((project_id, kwargs))
        return {"exportId": "vg_exp_test"}

    def get_export(self, project_id, export_id):
        self.export_poll_count += 1
        return {
            "status": "succeeded",
            "progressPercentage": 100,
            "downloadUrl": self.download_url,
            "downloadUrlExpiresAt": 9999999999,
            "exportFileId": "vg_file_test",
        }


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.content = b"{}" if json_data is not None else b""

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeSession:
    """Returns queued responses; records the requests it received."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.requests.append((method, url, json))
        return self._responses.pop(0)
