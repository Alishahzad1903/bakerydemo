import requests
from django.test import SimpleTestCase

from bakerydemo.videos.videogen.client import VideoGenClient
from bakerydemo.videos.videogen.exceptions import (
    VideoGenAuthenticationError,
    VideoGenBadRequestError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenNotFoundError,
    VideoGenPermissionError,
    VideoGenRateLimitError,
    VideoGenServerError,
)


class FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Returns a queue of responses (or raises queued exceptions)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.request_count = 0

    def request(self, method, url, **kwargs):
        self.request_count += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


def make_client(responses, **kwargs):
    kwargs.setdefault("backoff_factor", 0)  # no real sleeping in tests
    return VideoGenClient(
        "sk_test_key",
        session=FakeSession(responses),
        **kwargs,
    )


class ConfigurationTests(SimpleTestCase):
    def test_missing_key_raises_configuration_error(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient("")


class ErrorMappingTests(SimpleTestCase):
    def _assert_maps(self, status, exc_type, code=None):
        client = make_client(
            [FakeResponse(status, {"message": "boom", "code": code})],
            max_retries=0,
        )
        with self.assertRaises(exc_type) as ctx:
            client.get_workflow_run("vg_work_1")
        self.assertEqual(ctx.exception.status, status)
        self.assertEqual(ctx.exception.message, "boom")
        if code:
            self.assertEqual(ctx.exception.code, code)

    def test_400_bad_request(self):
        self._assert_maps(400, VideoGenBadRequestError, code="invalid_parameters")

    def test_401_authentication(self):
        self._assert_maps(401, VideoGenAuthenticationError, code="invalid_api_key")

    def test_403_permission(self):
        self._assert_maps(403, VideoGenPermissionError)

    def test_404_not_found(self):
        self._assert_maps(404, VideoGenNotFoundError)

    def test_429_rate_limit(self):
        self._assert_maps(429, VideoGenRateLimitError)

    def test_500_server_error(self):
        self._assert_maps(500, VideoGenServerError)


class RetryPolicyTests(SimpleTestCase):
    def test_idempotent_get_retries_on_500_then_succeeds(self):
        client = make_client(
            [
                FakeResponse(500, {"message": "try again"}),
                FakeResponse(
                    200,
                    {
                        "workflowRunId": "vg_work_1",
                        "status": "running",
                        "progressPercentage": 40,
                    },
                ),
            ],
            max_retries=3,
        )
        run = client.get_workflow_run("vg_work_1")
        self.assertEqual(run.status, "running")
        self.assertEqual(run.progress_percentage, 40)

    def test_mutating_post_not_retried_on_500(self):
        # A 5xx on a billed POST must NOT be retried (ambiguous outcome).
        session = FakeSession(
            [
                FakeResponse(500, {"message": "server error"}),
                FakeResponse(200, {"workflowRunId": "x", "projectId": "y"}),
            ]
        )
        client = VideoGenClient("sk", session=session, backoff_factor=0, max_retries=3)
        with self.assertRaises(VideoGenServerError):
            client.create_script_to_video(
                script="hi", aspect_ratio="16:9", visual_style={"type": "STOCK"}
            )
        self.assertEqual(session.request_count, 1)  # exactly one attempt

    def test_mutating_post_retried_on_429(self):
        # A 429 is safe to retry (request was rejected, not processed).
        session = FakeSession(
            [
                FakeResponse(429, {"message": "slow down"}),
                FakeResponse(200, {"workflowRunId": "x", "projectId": "y"}),
            ]
        )
        client = VideoGenClient("sk", session=session, backoff_factor=0, max_retries=3)
        result = client.create_script_to_video(
            script="hi", aspect_ratio="16:9", visual_style={"type": "STOCK"}
        )
        self.assertEqual(result["workflowRunId"], "x")
        self.assertEqual(session.request_count, 2)

    def test_network_error_on_get_retries_then_raises_connection_error(self):
        client = make_client(
            [
                requests.exceptions.ConnectionError("down"),
                requests.exceptions.ConnectionError("down"),
            ],
            max_retries=1,
        )
        with self.assertRaises(VideoGenConnectionError):
            client.get_workflow_run("vg_work_1")

    def test_network_error_on_post_not_retried(self):
        session = FakeSession([requests.exceptions.ConnectionError("down")])
        client = VideoGenClient("sk", session=session, backoff_factor=0, max_retries=3)
        with self.assertRaises(VideoGenConnectionError):
            client.export_project("vg_proj_1", quality="STANDARD")
        self.assertEqual(session.request_count, 1)


class PayloadTests(SimpleTestCase):
    def test_create_payload_has_cheap_shape(self):
        captured = {}

        class CapturingSession(FakeSession):
            def request(self, method, url, **kwargs):
                captured["json"] = kwargs.get("json")
                captured["headers"] = kwargs.get("headers")
                return super().request(method, url, **kwargs)

        session = CapturingSession(
            [FakeResponse(202, {"workflowRunId": "x", "projectId": "y"})]
        )
        client = VideoGenClient("sk_key", session=session, backoff_factor=0)
        client.create_script_to_video(
            script="Title. First sentence.",
            aspect_ratio={"width": 16, "height": 9},
            visual_style={"type": "STOCK"},
        )
        body = captured["json"]
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        self.assertEqual(body["visualStyle"], {"type": "STOCK"})
        # No avatar/presenter, no remix actions, no AI imagery.
        self.assertNotIn("actorEntityId", body)
        self.assertNotIn("avatarQuality", body)
        self.assertNotIn("remixActions", body)
        self.assertEqual(
            captured["headers"]["Authorization"], "Bearer sk_key"
        )
