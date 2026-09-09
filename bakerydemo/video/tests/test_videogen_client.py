from unittest import mock

from django.test import SimpleTestCase, override_settings

from bakerydemo.video.exceptions import (
    VideoGenAuthError,
    VideoGenBadRequestError,
    VideoGenConfigurationError,
    VideoGenNotFoundError,
    VideoGenRateLimitError,
    VideoGenServerError,
    VideoGenTransportError,
)
from bakerydemo.video.videogen import VideoGenClient


def _response(status_code, json_body=None, content=b"{}"):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.content = content
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    resp.text = ""
    return resp


class VideoGenClientConfigTests(SimpleTestCase):
    def test_requires_api_key(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient(api_key="")

    @override_settings(
        VIDEOGEN_API_KEY="k", VIDEOGEN_BASE_URL="https://custom.example/"
    )
    def test_from_settings_uses_base_url_override(self):
        client = VideoGenClient.from_settings()
        # Trailing slash is normalised away.
        self.assertEqual(client.base_url, "https://custom.example")
        self.assertEqual(client.api_key, "k")

    @override_settings(VIDEOGEN_API_KEY=None)
    def test_from_settings_without_key_raises(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient.from_settings()


class VideoGenClientRequestShapeTests(SimpleTestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.client = VideoGenClient(
            api_key="secret",
            base_url="https://api.videogen.io",
            session=self.session,
        )

    def test_script_to_video_sends_cheap_shape(self):
        self.session.request.return_value = _response(
            202, {"workflowRunId": "wf", "projectId": "p"}
        )
        self.client.create_script_to_video(script="Hello world.")

        _, kwargs = self.session.request.call_args
        args = self.session.request.call_args.args
        self.assertEqual(args[0], "POST")
        self.assertTrue(args[1].endswith("/v1/workflows/script-to-video"))
        body = kwargs["json"]
        # Stock footage only, 16:9 (object form), and NO AI/avatar/quality
        # upgrades, no remix actions, no avatar/actor, no scenes/b-roll.
        self.assertEqual(body["visualStyle"], {"type": "STOCK"})
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        self.assertNotIn("remixActions", body)
        self.assertNotIn("actorEntityId", body)
        self.assertNotIn("avatarQuality", body)
        self.assertNotIn("scenes", body)
        self.assertNotIn("featuredBRollFileIds", body)
        self.assertNotIn("quality", body)
        # Auth header carries the bearer token.
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")

    def test_export_requests_720p_hd(self):
        self.session.request.return_value = _response(202, {"exportId": "e"})
        self.client.export_project("proj_1")

        kwargs = self.session.request.call_args.kwargs
        args = self.session.request.call_args.args
        self.assertEqual(args[0], "POST")
        self.assertTrue(args[1].endswith("/v1/projects/proj_1/export"))
        self.assertEqual(kwargs["json"], {"quality": "STANDARD"})

    def test_download_file_does_not_send_bearer_token(self):
        self.session.get.return_value = _response(200, None, content=b"MP4")
        data = self.client.download_file("https://signed.example/x.mp4")
        self.assertEqual(data, b"MP4")
        # Plain GET on the signed URL, no Authorization header attached.
        kwargs = self.session.get.call_args.kwargs
        self.assertNotIn("headers", kwargs)


class VideoGenClientErrorMappingTests(SimpleTestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.client = VideoGenClient(api_key="secret", session=self.session)

    def _assert_maps(self, status, exc_type, code=None):
        self.session.request.return_value = _response(
            status, {"message": "boom", "code": code}
        )
        with self.assertRaises(exc_type) as ctx:
            self.client.get_workflow_run("wf")
        self.assertEqual(ctx.exception.status, status)
        self.assertEqual(ctx.exception.message, "boom")

    def test_401_maps_to_auth_error(self):
        self._assert_maps(401, VideoGenAuthError, "invalid_api_key")

    def test_400_maps_to_bad_request(self):
        self._assert_maps(400, VideoGenBadRequestError)

    def test_404_maps_to_not_found(self):
        self._assert_maps(404, VideoGenNotFoundError)

    def test_429_maps_to_rate_limit(self):
        self._assert_maps(429, VideoGenRateLimitError)

    def test_500_maps_to_server_error(self):
        self._assert_maps(500, VideoGenServerError)

    def test_network_error_maps_to_transport_error(self):
        import requests

        self.session.request.side_effect = requests.ConnectionError("down")
        with self.assertRaises(VideoGenTransportError):
            self.client.get_workflow_run("wf")
