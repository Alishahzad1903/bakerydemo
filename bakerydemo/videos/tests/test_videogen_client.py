from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase
from videogen.errors import VideoGenError as SdkVideoGenError

from bakerydemo.videos.exceptions import (
    VideoGenConfigurationError,
    VideoGenProviderError,
    VideoGenTimeoutError,
)
from bakerydemo.videos.videogen_client import VideoGenClient


class ClientConfigGuardTests(SimpleTestCase):
    def test_missing_api_key_raises_configuration_error(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient(api_key=None)

    def test_export_quality_above_1080p_is_refused(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenClient(api_key="k", export_quality="ULTRA_HIGH")

    def test_allowed_export_qualities_construct(self):
        for q in ("STANDARD", "HIGH", "FULL_HIGH"):
            VideoGenClient(api_key="k", export_quality=q)


class ProviderErrorTranslationTests(SimpleTestCase):
    def _client(self):
        return VideoGenClient(api_key="k")

    def test_stock_and_voice_only_request_shape(self):
        client = self._client()
        captured = {}

        def fake_script_to_video(**kwargs):
            captured.update(kwargs)
            return {"workflow_run_id": "vg_work_1", "project_id": "vg_proj_1"}

        client._client = mock.Mock()
        client._client.workflows.script_to_video.side_effect = fake_script_to_video
        client.start_script_to_video("hello")

        self.assertEqual(captured["visual_style"], {"type": "STOCK"})
        self.assertIsNone(captured["voice_id"])  # voice-only, no avatar
        self.assertNotIn("actor_entity_id", captured)  # no on-screen presenter

    def test_sdk_error_becomes_typed_provider_error(self):
        client = self._client()
        client._client = mock.Mock()
        client._client.workflows.script_to_video.side_effect = SdkVideoGenError(
            "boom", status=402, body={"message": "insufficient credits"}, request_id="req_9"
        )
        with self.assertRaises(VideoGenProviderError) as ctx:
            client.start_script_to_video("hello")
        self.assertEqual(ctx.exception.status, 402)
        self.assertEqual(ctx.exception.request_id, "req_9")
        self.assertIn("insufficient credits", str(ctx.exception))

    def test_timeout_becomes_typed_timeout_error(self):
        client = self._client()
        client._client = mock.Mock()
        client._client.poll_workflow_run.side_effect = TimeoutError("timed out")
        with self.assertRaises(VideoGenTimeoutError):
            client.wait_for_workflow("vg_work_1")

    def test_export_uses_configured_quality(self):
        client = VideoGenClient(api_key="k", export_quality="FULL_HIGH")
        client._client = mock.Mock()
        client._client.projects.export_project.return_value = {"export_id": "vg_expo_1"}
        self.assertEqual(client.start_export("vg_proj_1"), "vg_expo_1")
        _, kwargs = client._client.projects.export_project.call_args
        self.assertEqual(kwargs["quality"], "FULL_HIGH")

    def test_download_missing_url_and_file_id_raises_provider_error(self):
        client = self._client()
        with self.assertRaises(VideoGenProviderError):
            client.download_export({}, Path("unused.mp4"))
