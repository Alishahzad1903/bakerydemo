from unittest import mock

import httpx
from django.test import SimpleTestCase

from bakerydemo.videos import videogen_client as vg
from bakerydemo.videos.exceptions import (
    VideoGenAPIError,
    VideoGenConfigurationError,
    VideoGenProtocolError,
    VideoGenUnavailableError,
)

from .fakes import BoomTransport, json_response, stub_client, text_response

WORKFLOW_OK = {
    "workflowRunId": "wr_1",
    "projectId": "pr_1",
    "projectUrl": "https://app.videogen.io/p/pr_1",
    "remixActionIds": [],
}


class _ClientPatchMixin(SimpleTestCase):
    def use_client(self, client):
        patcher = mock.patch.object(vg, "get_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)


class StartScriptToVideoTests(_ClientPatchMixin):
    def test_success_requests_the_cheap_shape(self):
        client, transport = stub_client(json_response(200, WORKFLOW_OK))
        self.use_client(client)

        resp = vg.start_script_to_video("Tracking Wild Yeast. Yeasts and molds.")

        self.assertEqual(resp.workflow_run_id, "wr_1")
        self.assertEqual(resp.project_id, "pr_1")

        body = transport.last_request.body.value
        self.assertEqual(body["script"], "Tracking Wild Yeast. Yeasts and molds.")
        # Stock footage only, 16:9.
        self.assertEqual(body["visualStyle"]["type"], "STOCK")
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        # Voiceover only, no avatar, no billable remix actions or AI imagery.
        self.assertNotIn("actorEntityId", body)
        self.assertNotIn("remixActions", body)
        self.assertNotIn("quality", body)
        # Bearer auth was applied.
        self.assertEqual(transport.last_request.headers["authorization"], "Bearer test-token")

    def test_api_error_becomes_typed(self):
        client, _ = stub_client(
            json_response(400, {"message": "bad script", "code": "invalid_request"})
        )
        self.use_client(client)

        with self.assertRaises(VideoGenAPIError) as ctx:
            vg.start_script_to_video("x")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(ctx.exception.message, "bad script")

    def test_non_json_body_is_protocol_error(self):
        client, _ = stub_client(text_response(200, "<html>oops</html>"))
        self.use_client(client)
        with self.assertRaises(VideoGenProtocolError):
            vg.start_script_to_video("x")

    def test_missing_required_field_is_protocol_error(self):
        # Missing workflowRunId => pydantic decode failure.
        client, _ = stub_client(json_response(200, {"projectId": "pr_1"}))
        self.use_client(client)
        with self.assertRaises(VideoGenProtocolError):
            vg.start_script_to_video("x")

    def test_empty_ids_is_protocol_error(self):
        client, _ = stub_client(
            json_response(
                200,
                {
                    "workflowRunId": "",
                    "projectId": "",
                    "projectUrl": "u",
                    "remixActionIds": [],
                },
            )
        )
        self.use_client(client)
        with self.assertRaises(VideoGenProtocolError):
            vg.start_script_to_video("x")

    def test_transport_failure_is_unavailable(self):
        from videogen import VideogenClient

        transport = BoomTransport(httpx.ConnectError("refused"))
        client = VideogenClient(bearer_auth="t", custom_http_client=transport)
        self.use_client(client)
        with self.assertRaises(VideoGenUnavailableError):
            vg.start_script_to_video("x")


class ExportGuardTests(_ClientPatchMixin):
    def test_refuses_4k(self):
        client, _ = stub_client()
        self.use_client(client)
        with self.assertRaises(VideoGenConfigurationError):
            vg.start_export("pr_1", "ULTRA_HIGH")

    def test_export_success(self):
        client, transport = stub_client(json_response(200, {"exportId": "ex_1"}))
        self.use_client(client)
        resp = vg.start_export("pr_1", "HIGH")
        self.assertEqual(resp.export_id, "ex_1")
        self.assertEqual(transport.last_request.body.value["quality"], "HIGH")


class ConfigTests(SimpleTestCase):
    def test_missing_api_key_raises(self):
        # Force a rebuild with no key.
        with mock.patch.object(vg, "_client", None), self.settings(VIDEOGEN_API_KEY=None):
            with self.assertRaises(VideoGenConfigurationError):
                vg.get_client()
