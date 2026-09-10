from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from bakerydemo.video.client import VideoGenClient
from bakerydemo.video.exceptions import (
    VideoGenAuthError,
    VideoGenBadRequestError,
    VideoGenConfigurationError,
    VideoGenConnectionError,
    VideoGenForbiddenError,
    VideoGenNotFoundError,
    VideoGenRateLimitError,
    VideoGenServerError,
)


def _response(status_code, json_body=None, content=b"{}"):
    resp = mock.Mock(spec=requests.Response)
    resp.status_code = status_code
    resp.content = content
    resp.json = mock.Mock(return_value=json_body if json_body is not None else {})
    return resp


class VideoGenClientConfigTests(SimpleTestCase):
    def test_missing_api_key_raises_configuration_error(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient(api_key="")

    @override_settings(VIDEOGEN_API_KEY="")
    def test_from_settings_without_key_raises(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient.from_settings()

    def test_base_url_used_verbatim(self):
        session = mock.Mock(spec=requests.Session)
        session.request.return_value = _response(200, {"ok": True})
        client = VideoGenClient(
            api_key="k", base_url="https://proxy.internal/videogen/", session=session
        )
        client.get_me()
        called_url = session.request.call_args.args[1]
        # Trailing slash trimmed; host/path otherwise preserved.
        self.assertEqual(called_url, "https://proxy.internal/videogen/v1/me")

    def test_authorization_header_sent(self):
        session = mock.Mock(spec=requests.Session)
        session.request.return_value = _response(200, {"ok": True})
        client = VideoGenClient(api_key="secret-key", session=session)
        client.get_me()
        headers = session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer secret-key")


class VideoGenClientPayloadTests(SimpleTestCase):
    def setUp(self):
        self.session = mock.Mock(spec=requests.Session)
        self.client = VideoGenClient(api_key="k", session=self.session)

    def test_script_to_video_sends_cheap_shape(self):
        self.session.request.return_value = _response(
            202, {"workflowRunId": "wr_1", "projectId": "pr_1"}
        )
        self.client.create_script_to_video(script="Hello world.")
        kwargs = self.session.request.call_args.kwargs
        body = kwargs["json"]
        self.assertEqual(body["script"], "Hello world.")
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        self.assertEqual(body["visualStyle"], {"type": "STOCK"})
        # No avatar / presenter / ai-image / remix params leak in.
        for forbidden in ("actorEntityId", "avatarQuality", "remixActions", "quality"):
            self.assertNotIn(forbidden, body)

    def test_export_uses_720p_tier(self):
        self.session.request.return_value = _response(202, {"exportId": "ex_1"})
        self.client.export_project("pr_1")
        method, url = self.session.request.call_args.args[:2]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.videogen.io/v1/projects/pr_1/export")
        # 720p maps to the HIGH tier; never ULTRA_HIGH (4K).
        self.assertEqual(
            self.session.request.call_args.kwargs["json"], {"quality": "HIGH"}
        )


class VideoGenClientErrorMappingTests(SimpleTestCase):
    def setUp(self):
        self.session = mock.Mock(spec=requests.Session)
        self.client = VideoGenClient(api_key="k", session=self.session)

    def _assert_status_maps_to(self, status, exc_class):
        self.session.request.return_value = _response(
            status, {"message": "boom", "code": "some_code"}
        )
        with self.assertRaises(exc_class) as ctx:
            self.client.get_me()
        self.assertEqual(ctx.exception.status, status)
        self.assertEqual(ctx.exception.message, "boom")
        self.assertEqual(ctx.exception.code, "some_code")

    def test_400(self):
        self._assert_status_maps_to(400, VideoGenBadRequestError)

    def test_401(self):
        self._assert_status_maps_to(401, VideoGenAuthError)

    def test_403(self):
        self._assert_status_maps_to(403, VideoGenForbiddenError)

    def test_404(self):
        self._assert_status_maps_to(404, VideoGenNotFoundError)

    def test_429(self):
        self._assert_status_maps_to(429, VideoGenRateLimitError)

    def test_500(self):
        self._assert_status_maps_to(500, VideoGenServerError)

    def test_timeout_maps_to_connection_error(self):
        self.session.request.side_effect = requests.Timeout("slow")
        with self.assertRaises(VideoGenConnectionError):
            self.client.get_me()

    def test_connection_error(self):
        self.session.request.side_effect = requests.ConnectionError("down")
        with self.assertRaises(VideoGenConnectionError):
            self.client.get_me()
