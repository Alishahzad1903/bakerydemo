import json

import requests
from django.test import SimpleTestCase, override_settings

from bakerydemo.videos.videogen import (
    VideoGenAuthError,
    VideoGenBadRequestError,
    VideoGenClient,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenJobFailedError,
    VideoGenNotFoundError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenTimeoutError,
)


class FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = "" if body is None else json.dumps(body)
        self.content = self.text.encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Returns queued responses (or raises queued exceptions) in order."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(outcomes):
    session = FakeSession(outcomes)
    client = VideoGenClient("sk_test_key", base_url="https://example.test", session=session)
    return client, session


class ClientConfigTests(SimpleTestCase):
    def test_missing_api_key_raises_configuration_error(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient("")

    def test_base_url_trailing_slash_is_normalized(self):
        client, _ = make_client([])
        client.base_url = client.base_url  # no-op
        c2 = VideoGenClient("k", base_url="https://x.test/")
        self.assertEqual(c2.base_url, "https://x.test")

    @override_settings(VIDEOGEN_API_KEY="", VIDEOGEN_BASE_URL="")
    def test_from_settings_requires_key(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient.from_settings()

    @override_settings(VIDEOGEN_API_KEY="sk_x", VIDEOGEN_BASE_URL="https://override.test")
    def test_from_settings_uses_override_base_url(self):
        client = VideoGenClient.from_settings()
        self.assertEqual(client.base_url, "https://override.test")
        self.assertEqual(client.api_key, "sk_x")


class RequestErrorMappingTests(SimpleTestCase):
    def test_bearer_header_is_sent(self):
        client, session = make_client([FakeResponse(200, {"ok": True})])
        client.create_script_to_video("hello")
        _, _, kwargs = session.calls[0]
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk_test_key")

    def test_script_to_video_payload_shape(self):
        client, session = make_client(
            [FakeResponse(202, {"workflowRunId": "r", "projectId": "p"})]
        )
        client.create_script_to_video("Narrate this.")
        _, url, kwargs = session.calls[0]
        self.assertTrue(url.endswith("/v1/workflows/script-to-video"))
        body = kwargs["json"]
        self.assertEqual(body["script"], "Narrate this.")
        self.assertEqual(body["visualStyle"], {"type": "STOCK"})
        # aspectRatio must be a width/height ratio object, not a string.
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})

    def test_400_maps_to_bad_request(self):
        client, _ = make_client(
            [FakeResponse(400, {"message": "bad", "code": "invalid_parameters"})]
        )
        with self.assertRaises(VideoGenBadRequestError) as ctx:
            client.create_script_to_video("x")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_parameters")

    def test_401_maps_to_auth_error(self):
        client, _ = make_client([FakeResponse(401, {"message": "Unauthorized"})])
        with self.assertRaises(VideoGenAuthError):
            client.get_workflow_run("run_1")

    def test_403_maps_to_auth_error(self):
        client, _ = make_client([FakeResponse(403, {"message": "forbidden"})])
        with self.assertRaises(VideoGenAuthError):
            client.get_workflow_run("run_1")

    def test_404_maps_to_not_found(self):
        client, _ = make_client([FakeResponse(404, {"message": "nope"})])
        with self.assertRaises(VideoGenNotFoundError):
            client.get_workflow_run("run_1")

    def test_429_maps_to_rate_limit_with_retry_after(self):
        client, _ = make_client(
            [FakeResponse(429, {"message": "slow down"}, {"Retry-After": "12"})]
        )
        with self.assertRaises(VideoGenRateLimitError) as ctx:
            client.get_workflow_run("run_1")
        self.assertEqual(ctx.exception.retry_after, 12.0)

    def test_500_maps_to_server_error(self):
        client, _ = make_client([FakeResponse(500, {"message": "boom"})])
        with self.assertRaises(VideoGenServerError):
            client.get_workflow_run("run_1")

    def test_transport_failure_maps_to_connection_error(self):
        client, _ = make_client([requests.ConnectionError("dns")])
        with self.assertRaises(VideoGenConnectionError):
            client.get_workflow_run("run_1")


class PollingTests(SimpleTestCase):
    def test_poll_returns_on_success(self):
        client, _ = make_client(
            [
                FakeResponse(200, {"status": "processing", "progressPercentage": 40}),
                FakeResponse(200, {"status": "succeeded", "progressPercentage": 100}),
            ]
        )
        seen = []
        result = client.poll_workflow_run(
            "run_1", interval=0, on_progress=lambda p: seen.append(p["status"])
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(seen, ["processing", "succeeded"])

    def test_poll_raises_job_failed_with_provider_message(self):
        client, _ = make_client(
            [FakeResponse(200, {"status": "failed", "error": {"message": "no footage"}})]
        )
        with self.assertRaises(VideoGenJobFailedError) as ctx:
            client.poll_workflow_run("run_1", interval=0)
        self.assertIn("no footage", str(ctx.exception))

    def test_poll_cancelled_is_failure(self):
        client, _ = make_client([FakeResponse(200, {"status": "cancelled"})])
        with self.assertRaises(VideoGenJobFailedError):
            client.poll_project_export("proj", "exp", interval=0)

    def test_poll_times_out(self):
        client, _ = make_client(
            [FakeResponse(200, {"status": "processing"})] * 5
        )
        with self.assertRaises(VideoGenTimeoutError):
            client.poll_workflow_run("run_1", interval=0, timeout=0)
