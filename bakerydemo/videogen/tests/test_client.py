import io
import json
import urllib.error
from unittest import mock

from django.test import SimpleTestCase

from bakerydemo.videogen.client import VideoGenClient
from bakerydemo.videogen.exceptions import (
    VideoGenAPIError,
    VideoGenAuthError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenNotFoundError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenTimeoutError,
)


def http_error(status, body):
    return urllib.error.HTTPError(
        url="https://api.videogen.io/x",
        code=status,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(json.dumps(body).encode()),
    )


class ClientConfigTests(SimpleTestCase):
    def test_missing_api_key_raises_configuration_error(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient(api_key="")

    def test_base_url_override_is_used_verbatim(self):
        client = VideoGenClient(api_key="k", base_url="https://custom.example/api/")
        self.assertEqual(client.base_url, "https://custom.example/api")

    def test_default_base_url(self):
        client = VideoGenClient(api_key="k")
        self.assertEqual(client.base_url, "https://api.videogen.io")


class ClientErrorMappingTests(SimpleTestCase):
    def setUp(self):
        self.client = VideoGenClient(api_key="k")

    def _raise(self, exc):
        return mock.patch("urllib.request.urlopen", side_effect=exc)

    def test_401_maps_to_auth_error(self):
        with self._raise(http_error(401, {"message": "Unauthorized"})):
            with self.assertRaises(VideoGenAuthError) as ctx:
                self.client.get_workflow_run("wr")
        self.assertEqual(ctx.exception.status, 401)

    def test_403_maps_to_auth_error(self):
        with self._raise(http_error(403, {"message": "Forbidden"})):
            with self.assertRaises(VideoGenAuthError):
                self.client.get_workflow_run("wr")

    def test_404_maps_to_not_found(self):
        with self._raise(http_error(404, {"message": "nope"})):
            with self.assertRaises(VideoGenNotFoundError):
                self.client.get_workflow_run("wr")

    def test_429_maps_to_rate_limit(self):
        with self._raise(http_error(429, {"message": "slow"})):
            with self.assertRaises(VideoGenRateLimitError):
                self.client.get_workflow_run("wr")

    def test_500_maps_to_server_error(self):
        with self._raise(http_error(500, {"message": "boom"})):
            with self.assertRaises(VideoGenServerError):
                self.client.get_workflow_run("wr")

    def test_400_maps_to_generic_api_error_with_code(self):
        with self._raise(
            http_error(400, {"message": "bad", "code": "invalid_parameters"})
        ):
            with self.assertRaises(VideoGenAPIError) as ctx:
                self.client.get_workflow_run("wr")
        self.assertEqual(ctx.exception.code, "invalid_parameters")
        self.assertEqual(ctx.exception.status, 400)

    def test_timeout_maps_to_timeout_error(self):
        with self._raise(urllib.error.URLError(TimeoutError("timed out"))):
            with self.assertRaises(VideoGenTimeoutError):
                self.client.get_workflow_run("wr")

    def test_connection_error_maps_to_connection_error(self):
        with self._raise(urllib.error.URLError("no route")):
            with self.assertRaises(VideoGenConnectionError):
                self.client.get_workflow_run("wr")


class ClientRequestShapeTests(SimpleTestCase):
    def setUp(self):
        self.client = VideoGenClient(api_key="secret-key")

    def test_script_to_video_sends_stock_only_payload(self):
        captured = {}

        class FakeResp:
            status = 202

            def read(self):
                return json.dumps({"workflowRunId": "wr", "projectId": "pr"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data)
            captured["auth"] = request.get_header("Authorization")
            return FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = self.client.script_to_video(script="hello world")

        self.assertEqual(result["workflowRunId"], "wr")
        self.assertEqual(
            captured["url"],
            "https://api.videogen.io/v1/workflows/script-to-video",
        )
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"]["visualStyle"], {"type": "STOCK"})
        self.assertEqual(captured["body"]["script"], "hello world")
        # No AI imagery, no aspect-ratio override (defaults to 16:9).
        self.assertNotIn("aspectRatio", captured["body"])
        self.assertEqual(captured["auth"], "Bearer secret-key")

    def test_export_sends_quality_only(self):
        captured = {}

        class FakeResp:
            status = 202

            def read(self):
                return json.dumps({"exportId": "ex"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data)
            captured["url"] = request.full_url
            return FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.export_project("pr_1", quality="STANDARD")

        self.assertEqual(captured["body"], {"quality": "STANDARD"})
        self.assertTrue(captured["url"].endswith("/v1/projects/pr_1/export"))
