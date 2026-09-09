from django.test import SimpleTestCase

from bakerydemo.video.client import VideoGenClient
from bakerydemo.video.exceptions import (
    VideoGenAuthError,
    VideoGenBadRequestError,
    VideoGenConfigurationError,
    VideoGenNotFoundError,
    VideoGenRateLimitError,
    VideoGenServerError,
)

from .fakes import FakeResponse, FakeSession


class VideoGenClientTests(SimpleTestCase):
    def _client(self, responses, **kwargs):
        return VideoGenClient(
            "sk_test", session=FakeSession(responses), max_retries=0, **kwargs
        )

    def test_requires_api_key(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient("")

    def test_base_url_override_used_verbatim(self):
        session = FakeSession([FakeResponse(200, {"ok": True})])
        client = VideoGenClient(
            "sk_test", base_url="http://localhost:9999", session=session
        )
        client.get_workflow_run("vg_work_1")
        method, url, _ = session.requests[0]
        self.assertEqual(url, "http://localhost:9999/v1/workflows/runs/vg_work_1")

    def test_success_returns_parsed_json(self):
        client = self._client([FakeResponse(202, {"workflowRunId": "vg_work_9"})])
        result = client.create_script_to_video(script="hi")
        self.assertEqual(result["workflowRunId"], "vg_work_9")

    def test_script_payload_defaults_to_stock_and_voice_only(self):
        session = FakeSession([FakeResponse(202, {"workflowRunId": "x"})])
        client = VideoGenClient("sk_test", session=session)
        client.create_script_to_video(script="hello", aspect_ratio="16:9")
        _, _, payload = session.requests[0]
        self.assertEqual(payload["visualStyle"], {"type": "STOCK"})
        self.assertNotIn("actorEntityId", payload)
        self.assertEqual(payload["aspectRatio"], "16:9")

    def test_error_status_maps_to_typed_exception(self):
        cases = [
            (401, VideoGenAuthError),
            (403, VideoGenAuthError),
            (404, VideoGenNotFoundError),
            (429, VideoGenRateLimitError),
            (400, VideoGenBadRequestError),
            (500, VideoGenServerError),
        ]
        for status, exc_type in cases:
            with self.subTest(status=status):
                client = self._client(
                    [FakeResponse(status, {"message": "boom", "code": "x"})]
                )
                with self.assertRaises(exc_type) as ctx:
                    client.get_workflow_run("vg_work_1")
                self.assertEqual(ctx.exception.status_code, status)
                self.assertEqual(ctx.exception.message, "boom")
