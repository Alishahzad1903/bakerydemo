import httpx
from django.test import SimpleTestCase, override_settings

from bakerydemo.videos.exceptions import (
    VideoGenConfigError,
    VideoGenRequestError,
    VideoGenUnavailableError,
    VideoGenUnreadableError,
)
from bakerydemo.videos.videogen_client import build_client

from .support import json_response, service_raising, service_with

# A complete, valid StartWorkflowRunResponse wire body.
_START_OK = {
    "workflowRunId": "vg_work_1",
    "projectId": "vg_proj_1",
    "projectUrl": "https://app.videogen.io/p/1",
    "remixActionIds": [],
}


class BuildClientTests(SimpleTestCase):
    @override_settings(VIDEOGEN_API_KEY="")
    def test_missing_key_is_config_error(self):
        with self.assertRaises(VideoGenConfigError):
            build_client()


class ScriptToVideoRequestTests(SimpleTestCase):
    def test_builds_the_cheap_request_shape(self):
        service, transport = service_with(json_response(200, _START_OK))

        result = service.start_script_to_video("Tracking Wild Yeast. Yeasts grow.")

        # Return value decoded from the wire.
        self.assertEqual(result.workflow_run_id, "vg_work_1")
        self.assertEqual(result.project_id, "vg_proj_1")

        req = transport.last_request
        assert req is not None
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v1/workflows/script-to-video"))
        # Bearer token, not OAuth: the operation is the very first request.
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(req.headers["authorization"], "Bearer test-key")

        body = req.body.value
        self.assertEqual(body["script"], "Tracking Wild Yeast. Yeasts grow.")
        self.assertEqual(body["visualStyle"]["type"], "STOCK")
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        # Cheap shape: no avatar, no remix actions, no AI imagery knobs.
        self.assertNotIn("actorEntityId", body)
        self.assertNotIn("remixActions", body)
        self.assertNotIn("avatarQuality", body)
        self.assertNotIn("featuredBRollFileIds", body)

    def test_export_request_is_high_720p(self):
        service, transport = service_with(json_response(200, {"exportId": "vg_expo_1"}))

        result = service.export_project("vg_proj_1")

        self.assertEqual(result.export_id, "vg_expo_1")
        req = transport.last_request
        assert req is not None
        self.assertTrue(req.url.endswith("/v1/projects/vg_proj_1/export"))
        self.assertEqual(req.body.value["quality"], "HIGH")  # 720p, never ULTRA_HIGH


class ErrorTranslationTests(SimpleTestCase):
    def test_non_2xx_becomes_request_error(self):
        service, _ = service_with(
            json_response(422, {"message": "bad", "code": "invalid_request"})
        )
        with self.assertRaises(VideoGenRequestError) as ctx:
            service.start_script_to_video("hi")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_undecodable_body_is_unreadable(self):
        # Missing required members -> pydantic ValidationError on a 2xx.
        service, _ = service_with(json_response(200, {"workflowRunId": "only"}))
        with self.assertRaises(VideoGenUnreadableError):
            service.start_script_to_video("hi")

    def test_transport_failure_is_unavailable(self):
        service, _ = service_raising(httpx.ConnectError("refused"))
        with self.assertRaises(VideoGenUnavailableError):
            service.start_script_to_video("hi")
