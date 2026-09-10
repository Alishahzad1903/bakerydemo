import io
import json
import urllib.error
from unittest import mock

from django.test import SimpleTestCase

from bakerydemo.videos.videogen.client import VideoGenClient
from bakerydemo.videos.videogen.exceptions import (
    VideoGenAuthenticationError,
    VideoGenBadRequestError,
    VideoGenConnectionError,
    VideoGenError,
    VideoGenNotFoundError,
    VideoGenServerError,
)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(status: int, body: dict):
    return urllib.error.HTTPError(
        "https://api.videogen.io/x",
        status,
        "error",
        {},
        io.BytesIO(json.dumps(body).encode()),
    )


class VideoGenClientConfigTests(SimpleTestCase):
    def test_blank_api_key_raises_immediately(self):
        with self.assertRaises(VideoGenError):
            VideoGenClient("")

    def test_base_url_override_used_verbatim(self):
        client = VideoGenClient("k", base_url="https://custom.example/api/")
        self.assertEqual(client.base_url, "https://custom.example/api")


class VideoGenClientRequestTests(SimpleTestCase):
    def setUp(self):
        self.client = VideoGenClient("sk_test", max_retries=2)
        sleep_patcher = mock.patch(
            "bakerydemo.videos.videogen.client.time.sleep", return_value=None
        )
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def _patch_urlopen(self, side_effect):
        patcher = mock.patch("urllib.request.urlopen", side_effect=side_effect)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def test_successful_request_parses_json_and_sets_bearer(self):
        mocked = self._patch_urlopen(
            [_FakeResponse(b'{"workflowRunId": "r1", "projectId": "p1"}')]
        )
        result = self.client.create_script_to_video(
            script="hi", visual_style={"type": "STOCK"}, aspect_ratio="16:9"
        )
        self.assertEqual(result["workflowRunId"], "r1")
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer sk_test")
        self.assertEqual(request.method, "POST")
        # Must not use the default Python-urllib agent (rejected with 403).
        self.assertEqual(
            request.get_header("User-agent"), "bakerydemo-videogen-integration/1.0"
        )

    def test_401_maps_to_authentication_error_with_code(self):
        self._patch_urlopen(
            [_http_error(401, {"message": "bad key", "code": "invalid_api_key"})]
        )
        with self.assertRaises(VideoGenAuthenticationError) as ctx:
            self.client.get_workflow_run("r1")
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, "invalid_api_key")

    def test_400_maps_to_bad_request(self):
        self._patch_urlopen([_http_error(400, {"message": "bad"})])
        with self.assertRaises(VideoGenBadRequestError):
            self.client.get_workflow_run("r1")

    def test_404_maps_to_not_found(self):
        self._patch_urlopen([_http_error(404, {"message": "nope"})])
        with self.assertRaises(VideoGenNotFoundError):
            self.client.get_project_export("p1", "e1")

    def test_429_is_retried_then_succeeds(self):
        self._patch_urlopen(
            [
                _http_error(429, {"message": "slow down"}),
                _FakeResponse(b'{"status": "succeeded"}'),
            ]
        )
        result = self.client.get_workflow_run("r1")
        self.assertEqual(result["status"], "succeeded")

    def test_500_exhausts_retries_and_raises_server_error(self):
        self._patch_urlopen([_http_error(500, {"message": "boom"})] * 5)
        with self.assertRaises(VideoGenServerError):
            self.client.get_workflow_run("r1")

    def test_connection_error_maps_to_connection_error(self):
        self._patch_urlopen([urllib.error.URLError("dns fail")] * 5)
        with self.assertRaises(VideoGenConnectionError):
            self.client.get_workflow_run("r1")
