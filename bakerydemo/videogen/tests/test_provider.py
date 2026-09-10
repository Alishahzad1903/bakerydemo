import io
from unittest import mock

from django.test import SimpleTestCase, override_settings
from videogen import VideoGenError

from bakerydemo.videogen.exceptions import (
    VideoGenAPIError,
    VideoGenConfigurationError,
    VideoGenProductionError,
)
from bakerydemo.videogen.provider import VideoGenProvider


@override_settings(VIDEOGEN_API_KEY="test-key", VIDEOGEN_BASE_URL=None)
class ProviderCostShapeTests(SimpleTestCase):
    """The provider must only ever ask VideoGen for the cheap, allowed shape."""

    def _patch(self):
        return (
            mock.patch("bakerydemo.videogen.provider.VideoGen"),
            mock.patch("bakerydemo.videogen.provider.poll_workflow_run"),
            mock.patch("bakerydemo.videogen.provider.poll_project_export"),
        )

    def test_requests_only_the_allowed_shape(self):
        with self._patch()[0] as MockVG, self._patch()[1] as poll_run, self._patch()[
            2
        ] as poll_export:
            client = MockVG.return_value
            client.workflows.script_to_video.return_value = {
                "workflow_run_id": "vg_work_1",
                "project_id": "vg_proj_1",
            }
            client.projects.export_project.return_value = {"export_id": "vg_exp_1"}
            poll_run.return_value = {"status": "succeeded", "progress_percentage": 100}
            poll_export.return_value = {
                "status": "succeeded",
                "download_url": "https://cdn.example/v.mp4",
                "export_file_id": "vg_file_1",
            }

            provider = VideoGenProvider()
            started = provider.start_script_to_video("Title. Sentence.")
            provider.wait_for_run(started.workflow_run_id)
            finished = provider.export_video(started.project_id)

            # Script sent verbatim with the STOCK (non-AI) visual style and
            # nothing else: no remixActions, no quality/resolution override.
            client.workflows.script_to_video.assert_called_once_with(
                script="Title. Sentence.",
                visual_style={"type": "STOCK"},
            )
            _, kwargs = client.workflows.script_to_video.call_args
            self.assertEqual(kwargs["visual_style"]["type"], "STOCK")
            self.assertNotEqual(kwargs["visual_style"]["type"], "AI_IMAGE")
            self.assertNotIn("remix_actions", kwargs)
            self.assertNotIn("remixActions", kwargs)
            # Exactly ONE export, and it carries no resolution/quality override.
            client.projects.export_project.assert_called_once_with(
                project_id="vg_proj_1"
            )
            self.assertEqual(finished.download_url, "https://cdn.example/v.mp4")

            # None of the billable add-ons are ever invoked.
            client.projects.remix_project.assert_not_called()
            client.tools.generate_image.assert_not_called()
            client.tools.generate_video_clip.assert_not_called()
            client.tools.text_to_speech.assert_not_called()
            client.tools.generate_avatar.assert_not_called()
            client.tools.generate_music.assert_not_called()
            client.tools.upscale_video.assert_not_called()
            client.tools.image_3d_effect.assert_not_called()

    def test_base_url_override_is_passed_through(self):
        with override_settings(VIDEOGEN_BASE_URL="https://vg.internal"):
            with mock.patch("bakerydemo.videogen.provider.VideoGen") as MockVG:
                VideoGenProvider()
                _, kwargs = MockVG.call_args
                self.assertEqual(kwargs["base_url"], "https://vg.internal")

    def test_failed_run_raises_production_error(self):
        with mock.patch("bakerydemo.videogen.provider.VideoGen"), mock.patch(
            "bakerydemo.videogen.provider.poll_workflow_run"
        ) as poll_run:
            poll_run.return_value = {
                "status": "failed",
                "error": {"message": "render exploded"},
            }
            provider = VideoGenProvider()
            with self.assertRaises(VideoGenProductionError) as ctx:
                provider.wait_for_run("vg_work_1")
            self.assertIn("render exploded", str(ctx.exception))

    def test_http_error_is_translated(self):
        with mock.patch("bakerydemo.videogen.provider.VideoGen") as MockVG:
            client = MockVG.return_value
            client.workflows.script_to_video.side_effect = VideoGenError(
                "boom", status=500, body=None, request_id="req_1"
            )
            provider = VideoGenProvider()
            with self.assertRaises(VideoGenAPIError) as ctx:
                provider.start_script_to_video("Title.")
            self.assertEqual(ctx.exception.status, 500)
            self.assertEqual(ctx.exception.request_id, "req_1")


class ProviderConfigTests(SimpleTestCase):
    @override_settings(VIDEOGEN_API_KEY="")
    def test_missing_api_key_raises_configuration_error(self):
        with self.assertRaises(VideoGenConfigurationError):
            VideoGenProvider()


@override_settings(VIDEOGEN_API_KEY="test-key")
class ProviderDownloadTests(SimpleTestCase):
    def test_download_streams_bytes(self):
        payload = b"FAKEMP4BYTES" * 100
        resp = mock.MagicMock()
        resp.iter_bytes.return_value = [payload[:100], payload[100:]]
        resp.raise_for_status.return_value = None
        stream_cm = mock.MagicMock()
        stream_cm.__enter__.return_value = resp
        with mock.patch("bakerydemo.videogen.provider.VideoGen"), mock.patch(
            "bakerydemo.videogen.provider.httpx.stream", return_value=stream_cm
        ):
            provider = VideoGenProvider()
            buf = io.BytesIO()
            written = provider.download_to("https://cdn.example/v.mp4", buf)
            self.assertEqual(written, len(payload))
            self.assertEqual(buf.getvalue(), payload)
