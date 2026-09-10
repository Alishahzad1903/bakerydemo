from django.test import SimpleTestCase

from bakerydemo.video.videogen import (
    VideoGenAuthenticationError,
    VideoGenBadRequestError,
    VideoGenClient,
    VideoGenError,
    VideoGenNotFoundError,
    VideoGenPermissionError,
    VideoGenRateLimitError,
    VideoGenServerError,
)
from bakerydemo.video.videogen.client import DEFAULT_BASE_URL

from .fakes import FakeResponse, FakeSession


def make_client(responses, *, base_url=None):
    return VideoGenClient(
        api_key="sk_test_key",
        base_url=base_url,
        session=FakeSession(responses),
    )


class ClientConfigTests(SimpleTestCase):
    def test_missing_api_key_raises_typed_error(self):
        with self.assertRaises(VideoGenAuthenticationError):
            VideoGenClient(api_key="")

    def test_default_base_url(self):
        client = make_client([])
        self.assertEqual(client.base_url, DEFAULT_BASE_URL)

    def test_base_url_override_used_verbatim(self):
        client = make_client([], base_url="https://proxy.example/api/")
        # Trailing slash stripped, otherwise verbatim.
        self.assertEqual(client.base_url, "https://proxy.example/api")


class CreateScriptToVideoTests(SimpleTestCase):
    def test_builds_cheap_stock_voice_payload(self):
        session = FakeSession(
            [FakeResponse(202, {"workflowRunId": "vg_wfr_1", "projectId": "vg_proj_1"})]
        )
        client = VideoGenClient(api_key="sk", session=session)
        result = client.create_script_to_video("Narration here.")

        self.assertEqual(result.workflow_run_id, "vg_wfr_1")
        self.assertEqual(result.project_id, "vg_proj_1")

        method, url, body, headers = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/v1/workflows/script-to-video"))
        self.assertEqual(body["visualStyle"], {"type": "STOCK"})
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        self.assertEqual(body["script"], "Narration here.")
        # No AI imagery / avatar / remix knobs are ever requested.
        self.assertNotIn("remixActions", body)
        self.assertNotIn("actorEntityId", body)
        self.assertEqual(headers["Authorization"], "Bearer sk")

    def test_unexpected_response_raises(self):
        client = make_client([FakeResponse(202, {"projectId": "vg_proj_1"})])
        with self.assertRaises(VideoGenError):
            client.create_script_to_video("x")


class ExportTests(SimpleTestCase):
    def test_start_export_omits_quality_by_default(self):
        session = FakeSession([FakeResponse(202, {"exportId": "vg_exp_1"})])
        client = VideoGenClient(api_key="sk", session=session)
        export_id = client.start_export("vg_proj_1")

        self.assertEqual(export_id, "vg_exp_1")
        _, url, body, _ = session.calls[0]
        self.assertTrue(url.endswith("/v1/projects/vg_proj_1/export"))
        # No documented quality constant is accepted by the live API, so the
        # field is omitted (provider default) unless explicitly configured.
        self.assertNotIn("quality", body)
        # Watermark/end-screen left at AUTO (no Pro-only NONE).
        self.assertNotIn("watermarkMode", body)

    def test_start_export_includes_quality_when_configured(self):
        session = FakeSession([FakeResponse(202, {"exportId": "vg_exp_1"})])
        client = VideoGenClient(api_key="sk", session=session)
        client.start_export("vg_proj_1", quality="RES_720P")
        _, _, body, _ = session.calls[0]
        self.assertEqual(body["quality"], "RES_720P")

    def test_get_export_parses_download_url(self):
        client = make_client(
            [
                FakeResponse(
                    200,
                    {
                        "exportId": "vg_exp_1",
                        "status": "succeeded",
                        "progressPercentage": 100,
                        "downloadUrl": "https://signed/video.mp4",
                        "exportFileId": "vg_file_1",
                    },
                )
            ]
        )
        export = client.get_export("vg_proj_1", "vg_exp_1")
        self.assertEqual(export.status, "succeeded")
        self.assertEqual(export.download_url, "https://signed/video.mp4")
        self.assertTrue(export.is_terminal)


class ErrorMappingTests(SimpleTestCase):
    def _assert_maps(self, status, exc_type):
        client = make_client(
            [FakeResponse(status, {"message": "boom", "code": "some_code"})]
        )
        with self.assertRaises(exc_type) as ctx:
            client.get_workflow_run("vg_wfr_1")
        self.assertEqual(ctx.exception.status, status)
        self.assertEqual(ctx.exception.message, "boom")

    def test_400_bad_request(self):
        self._assert_maps(400, VideoGenBadRequestError)

    def test_401_authentication(self):
        self._assert_maps(401, VideoGenAuthenticationError)

    def test_403_permission(self):
        self._assert_maps(403, VideoGenPermissionError)

    def test_404_not_found(self):
        self._assert_maps(404, VideoGenNotFoundError)

    def test_429_rate_limit(self):
        self._assert_maps(429, VideoGenRateLimitError)

    def test_500_server_error(self):
        self._assert_maps(500, VideoGenServerError)

    def test_workflow_failed_carries_error_message(self):
        client = make_client(
            [
                FakeResponse(
                    200,
                    {
                        "workflowRunId": "vg_wfr_1",
                        "status": "failed",
                        "progressPercentage": 40,
                        "error": {"message": "script too long"},
                    },
                )
            ]
        )
        run = client.get_workflow_run("vg_wfr_1")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_message, "script too long")


class DownloadTests(SimpleTestCase):
    def test_download_returns_bytes(self):
        client = make_client([FakeResponse(200, content=b"MP4")])
        self.assertEqual(client.download_file("https://signed/video.mp4"), b"MP4")

    def test_download_http_error_raises(self):
        client = make_client([FakeResponse(404, content=b"nope")])
        with self.assertRaises(VideoGenError):
            client.download_file("https://signed/video.mp4")
