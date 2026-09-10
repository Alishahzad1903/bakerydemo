import json as jsonlib

import requests
from django.test import SimpleTestCase

from bakerydemo.videogen.client import VideoGenClient
from bakerydemo.videogen.exceptions import (
    VideoGenAuthenticationError,
    VideoGenBadRequestError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenNotFoundError,
    VideoGenServerError,
    VideoGenTimeoutError,
)


class FakeResponse:
    def __init__(self, status_code, body=None, *, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.reason = "reason"
        self.text = "" if body is None else jsonlib.dumps(body)
        self.content = self.text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Session stub returning queued responses or raising queued exceptions."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(outcomes, **kwargs):
    kwargs.setdefault("backoff_factor", 0)  # no real sleeping in tests
    return VideoGenClient("sk_test", session=FakeSession(outcomes), **kwargs)


class ClientConfigTests(SimpleTestCase):
    def test_requires_api_key(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient("")

    def test_uses_default_base_url(self):
        client = VideoGenClient("sk_test")
        self.assertEqual(client.base_url, "https://api.videogen.io")

    def test_base_url_override_is_verbatim(self):
        client = VideoGenClient("sk_test", "https://proxy.internal/vg/")
        self.assertEqual(client.base_url, "https://proxy.internal/vg")


class ClientSuccessTests(SimpleTestCase):
    def test_create_script_to_video_posts_cheap_shape(self):
        client = make_client([FakeResponse(202, {"workflowRunId": "wr_1", "projectId": "p_1"})])
        result = client.create_script_to_video(script="Hello world.")
        self.assertEqual(result["workflowRunId"], "wr_1")
        method, url, body = client.session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/v1/workflows/script-to-video"))
        self.assertEqual(body["visualStyle"], {"type": "STOCK"})
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        self.assertNotIn("actorEntityId", body)
        self.assertNotIn("remixActions", body)

    def test_export_project_requests_720p_standard(self):
        client = make_client([FakeResponse(202, {"exportId": "ex_1"})])
        client.export_project("p_1")
        _, url, body = client.session.calls[0]
        self.assertTrue(url.endswith("/v1/projects/p_1/export"))
        self.assertEqual(body, {"quality": "STANDARD"})


class ClientErrorMappingTests(SimpleTestCase):
    def test_400_maps_to_bad_request(self):
        client = make_client([FakeResponse(400, {"message": "bad", "code": "invalid_parameters"})])
        with self.assertRaises(VideoGenBadRequestError) as ctx:
            client.export_project("p_1")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_parameters")

    def test_401_maps_to_auth_error(self):
        client = make_client([FakeResponse(401, {"message": "Unauthorized"})])
        with self.assertRaises(VideoGenAuthenticationError):
            client.export_project("p_1")

    def test_404_maps_to_not_found(self):
        client = make_client([FakeResponse(404, {"message": "nope"})])
        with self.assertRaises(VideoGenNotFoundError):
            client.get_workflow_run("wr_x")

    def test_timeout_is_typed_and_post_not_retried(self):
        client = make_client([requests.Timeout()])
        with self.assertRaises(VideoGenTimeoutError):
            client.export_project("p_1")  # POST => single attempt
        self.assertEqual(len(client.session.calls), 1)

    def test_connection_error_is_typed(self):
        client = make_client([requests.ConnectionError("boom")])
        with self.assertRaises(VideoGenConnectionError):
            client.export_project("p_1")


class ClientRetryTests(SimpleTestCase):
    def test_idempotent_get_retries_on_500_then_succeeds(self):
        client = make_client(
            [FakeResponse(500, {"message": "oops"}), FakeResponse(200, {"status": "running"})],
            max_retries=3,
        )
        result = client.get_workflow_run("wr_1")
        self.assertEqual(result["status"], "running")
        self.assertEqual(len(client.session.calls), 2)

    def test_idempotent_get_retries_on_429(self):
        client = make_client(
            [FakeResponse(429, {"message": "slow down"}), FakeResponse(200, {"status": "running"})],
            max_retries=3,
        )
        self.assertEqual(client.get_workflow_run("wr_1")["status"], "running")

    def test_retries_exhausted_raises(self):
        client = make_client(
            [FakeResponse(500, {"message": "oops"})] * 3,
            max_retries=3,
        )
        with self.assertRaises(VideoGenServerError):
            client.get_workflow_run("wr_1")
        self.assertEqual(len(client.session.calls), 3)
