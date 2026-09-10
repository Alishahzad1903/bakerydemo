import httpx
from django.test import SimpleTestCase

from bakerydemo.videos.exceptions import (
    VideoGenAPIError,
    VideoGenUnavailableError,
    VideoGenUnreadableError,
)
from bakerydemo.videos.service import VideoGenService

from .stubs import json_response, make_client, text_response


def _start_workflow_body():
    return {
        "workflowRunId": "vg_work_abc",
        "projectId": "vg_proj_abc",
        "projectUrl": "https://app.videogen.io/projects/vg_proj_abc",
        "remixActionIds": [],
    }


def _workflow_run_body(status="running", progress=42.0, error=None):
    return {
        "workflowRunId": "vg_work_abc",
        "status": status,
        "workflowType": "SCRIPT_TO_VIDEO",
        "progressPercentage": progress,
        "attemptIndex": 0,
        "projectId": "vg_proj_abc",
        "projectUrl": "https://app.videogen.io/projects/vg_proj_abc",
        "error": error,
    }


def _project_export_body(status="succeeded", progress=100.0, download_url="https://cdn/v.mp4", error=None):
    return {
        "exportId": "vg_expo_abc",
        "projectId": "vg_proj_abc",
        "status": status,
        "progressPercentage": progress,
        "attemptIndex": 0,
        "downloadUrl": download_url,
        "downloadUrlExpiresAt": 1893456000 if download_url else None,
        "thumbnailUrl": None,
        "thumbnailUrlExpiresAt": None,
        "exportFileId": "vg_file_abc" if download_url else None,
        "file": None,
        "error": error,
    }


class StartScriptToVideoTests(SimpleTestCase):
    def test_success_returns_ids_and_sends_the_cheap_shape(self):
        client, transport = make_client(json_response(200, _start_workflow_body()))
        result = VideoGenService(client=client).start_script_to_video("Hello world.")

        self.assertEqual(result.workflow_run_id, "vg_work_abc")
        self.assertEqual(result.project_id, "vg_proj_abc")

        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v1/workflows/script-to-video"), req.url)
        # Wire aliases, and only the cheap "voice over stock footage, 16:9" shape.
        body = req.body.value
        self.assertEqual(body["script"], "Hello world.")
        self.assertEqual(body["visualStyle"], {"type": "STOCK"})
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        # No avatar/actor, no AI-image quality, no remix actions.
        self.assertNotIn("actorEntityId", body)
        self.assertNotIn("remixActions", body)

    def test_api_error_becomes_typed_error_with_status(self):
        client, _ = make_client(json_response(403, {"message": "nope", "code": "not_authorized"}))
        with self.assertRaises(VideoGenAPIError) as ctx:
            VideoGenService(client=client).start_script_to_video("hi")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_truncated_success_body_is_unreadable(self):
        # Missing required projectId -> decode fails -> outcome unknown.
        body = _start_workflow_body()
        del body["projectId"]
        client, _ = make_client(json_response(200, body))
        with self.assertRaises(VideoGenUnreadableError):
            VideoGenService(client=client).start_script_to_video("hi")

    def test_transport_failure_is_unavailable(self):
        client, _ = make_client(httpx.ConnectError("refused"))
        with self.assertRaises(VideoGenUnavailableError):
            VideoGenService(client=client).start_script_to_video("hi")

    def test_non_json_error_body_is_typed_error(self):
        client, _ = make_client(text_response(500, "upstream boom"))
        with self.assertRaises(VideoGenAPIError) as ctx:
            VideoGenService(client=client).start_script_to_video("hi")
        self.assertEqual(ctx.exception.status_code, 500)


class GetWorkflowRunTests(SimpleTestCase):
    def test_maps_status_and_progress(self):
        client, transport = make_client(json_response(200, _workflow_run_body(progress=55.5)))
        snap = VideoGenService(client=client).get_workflow_run("vg_work_abc")
        self.assertEqual(snap.status, "running")
        self.assertEqual(snap.progress_percentage, 55.5)
        self.assertFalse(snap.is_terminal)
        self.assertTrue(transport.last_request.url.endswith("/v1/workflows/runs/vg_work_abc"))

    def test_failed_run_surfaces_error_message(self):
        body = _workflow_run_body(
            status="failed", progress=0.0, error={"message": "bad script", "code": "invalid_request"}
        )
        client, _ = make_client(json_response(200, body))
        snap = VideoGenService(client=client).get_workflow_run("vg_work_abc")
        self.assertEqual(snap.status, "failed")
        self.assertTrue(snap.is_terminal)
        self.assertIn("bad script", snap.error_message)

    def test_type_mismatch_is_unreadable(self):
        body = _workflow_run_body()
        body["progressPercentage"] = "not-a-number"
        client, _ = make_client(json_response(200, body))
        with self.assertRaises(VideoGenUnreadableError):
            VideoGenService(client=client).get_workflow_run("vg_work_abc")


class ExportProjectTests(SimpleTestCase):
    def test_requests_single_720p_export(self):
        client, transport = make_client(json_response(200, {"exportId": "vg_expo_abc"}))
        export_id = VideoGenService(client=client).export_project("vg_proj_abc")

        self.assertEqual(export_id, "vg_expo_abc")
        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v1/projects/vg_proj_abc/export"), req.url)
        # HIGH == 720p; never ULTRA_HIGH (4K).
        self.assertEqual(req.body.value["quality"], "HIGH")


class GetProjectExportTests(SimpleTestCase):
    def test_maps_download_url(self):
        client, _ = make_client(json_response(200, _project_export_body()))
        snap = VideoGenService(client=client).get_project_export("vg_proj_abc", "vg_expo_abc")
        self.assertTrue(snap.is_succeeded)
        self.assertEqual(snap.download_url, "https://cdn/v.mp4")

    def test_pending_export_has_no_url(self):
        client, _ = make_client(
            json_response(200, _project_export_body(status="running", progress=10.0, download_url=None))
        )
        snap = VideoGenService(client=client).get_project_export("vg_proj_abc", "vg_expo_abc")
        self.assertFalse(snap.is_terminal)
        self.assertIsNone(snap.download_url)
