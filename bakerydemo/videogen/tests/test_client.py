import json as jsonlib

import requests
from django.test import SimpleTestCase, override_settings

from bakerydemo.videogen.client import DEFAULT_BASE_URL, VideoGenClient
from bakerydemo.videogen.exceptions import (
    VideoGenAPIError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenExportError,
    VideoGenTimeoutError,
    VideoGenWorkflowError,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        if body is not None:
            self._text = jsonlib.dumps(body)
        else:
            self._text = text if text is not None else ""
        self.content = self._text.encode()

    @property
    def text(self):
        return self._text

    def json(self):
        if not self._text:
            raise ValueError("no content")
        return jsonlib.loads(self._text)


class FakeSession:
    """Serves queued responses; records requests. Optionally raises."""

    def __init__(self):
        self.requests = []
        self._queue = []
        self.raise_on_request = None
        self.get_responses = []

    def queue(self, *responses):
        self._queue.extend(responses)

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.requests.append(
            {"method": method, "url": url, "json": json, "headers": headers}
        )
        if self.raise_on_request is not None:
            raise self.raise_on_request
        return self._queue.pop(0)

    def get(self, url, timeout=None, stream=None):
        self.requests.append({"method": "GET", "url": url})
        return self.get_responses.pop(0)


def make_client(session, **kwargs):
    kwargs.setdefault("api_key", "sk_test_key")
    kwargs.setdefault("sleep", lambda _s: None)
    kwargs.setdefault("poll_interval", 0)
    return VideoGenClient(session=session, **kwargs)


@override_settings(VIDEOGEN_API_KEY="", VIDEOGEN_BASE_URL="")
class ConfigurationTests(SimpleTestCase):
    def test_missing_api_key_raises_configuration_error(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient()

    def test_explicit_key_allows_construction(self):
        client = VideoGenClient(api_key="sk_x")
        self.assertEqual(client.base_url, DEFAULT_BASE_URL)

    def test_base_url_override_used_verbatim(self):
        client = VideoGenClient(api_key="sk_x", base_url="https://vg.internal/api/")
        self.assertEqual(client.base_url, "https://vg.internal/api")


class RequestTests(SimpleTestCase):
    def test_create_script_to_video_sends_cheap_shape(self):
        session = FakeSession()
        session.queue(
            FakeResponse(202, {"workflowRunId": "vg_work_1", "projectId": "vg_proj_1"})
        )
        client = make_client(session)
        result = client.create_script_to_video(script="Title. Sentence.")

        self.assertEqual(result["workflowRunId"], "vg_work_1")
        sent = session.requests[0]
        self.assertEqual(sent["method"], "POST")
        self.assertTrue(sent["url"].endswith("/v1/workflows/script-to-video"))
        self.assertEqual(sent["json"]["script"], "Title. Sentence.")
        # Cheap shape: STOCK footage (never AI), 16:9, no actor/avatar, no remix.
        self.assertEqual(sent["json"]["visualStyle"], {"type": "STOCK"})
        self.assertEqual(sent["json"]["aspectRatio"], {"width": 16, "height": 9})
        self.assertNotIn("actorEntityId", sent["json"])
        self.assertNotIn("remixActions", sent["json"])
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk_test_key")

    def test_export_requests_standard_quality(self):
        session = FakeSession()
        session.queue(FakeResponse(202, {"exportId": "vg_expo_1"}))
        client = make_client(session)
        client.export_project("vg_proj_1")
        self.assertEqual(session.requests[0]["json"], {"quality": "STANDARD"})

    def test_api_error_carries_status_and_code(self):
        session = FakeSession()
        session.queue(
            FakeResponse(
                400,
                {"error": {"message": "bad script", "code": "invalid_script"}},
            )
        )
        client = make_client(session)
        with self.assertRaises(VideoGenAPIError) as ctx:
            client.create_script_to_video(script="x")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.code, "invalid_script")

    def test_connection_error_wrapped(self):
        session = FakeSession()
        session.raise_on_request = requests.ConnectionError("boom")
        client = make_client(session)
        with self.assertRaises(VideoGenConnectionError):
            client.get_workflow_run("vg_work_1")


class PollingTests(SimpleTestCase):
    def test_poll_workflow_run_succeeds(self):
        session = FakeSession()
        session.queue(
            FakeResponse(200, {"status": "pending", "progressPercentage": 0}),
            FakeResponse(200, {"status": "running", "progressPercentage": 50}),
            FakeResponse(200, {"status": "succeeded", "progressPercentage": 100}),
        )
        seen = []
        client = make_client(session)
        run = client.poll_workflow_run("vg_work_1", on_progress=seen.append)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(len(seen), 3)

    def test_poll_workflow_run_failed_raises_with_code(self):
        session = FakeSession()
        session.queue(
            FakeResponse(
                200,
                {"status": "failed", "error": {"message": "nope", "code": "boom"}},
            )
        )
        client = make_client(session)
        with self.assertRaises(VideoGenWorkflowError) as ctx:
            client.poll_workflow_run("vg_work_1")
        self.assertEqual(ctx.exception.code, "boom")

    def test_poll_export_failed_raises(self):
        session = FakeSession()
        session.queue(FakeResponse(200, {"status": "failed"}))
        client = make_client(session)
        with self.assertRaises(VideoGenExportError):
            client.poll_project_export("vg_proj_1", "vg_expo_1")

    def test_poll_times_out(self):
        session = FakeSession()
        # Always "running" — never terminal.
        for _ in range(10):
            session.queue(FakeResponse(200, {"status": "running"}))
        # A clock that jumps past the budget after the first check.
        ticks = iter([0.0, 100.0, 200.0, 300.0])
        client = make_client(session, poll_timeout=10, clock=lambda: next(ticks))
        with self.assertRaises(VideoGenTimeoutError):
            client.poll_workflow_run("vg_work_1")

    def test_download_returns_bytes(self):
        session = FakeSession()
        session.get_responses.append(FakeResponse(200, text="MP4BYTES"))
        client = make_client(session)
        self.assertEqual(client.download("https://signed.example/x.mp4"), b"MP4BYTES")

    def test_download_error_raises(self):
        session = FakeSession()
        session.get_responses.append(FakeResponse(404, text="gone"))
        client = make_client(session)
        with self.assertRaises(VideoGenAPIError):
            client.download("https://signed.example/x.mp4")
