import httpx
from django.test import SimpleTestCase

from bakerydemo.videos import videogen_service
from bakerydemo.videos.exceptions import (
    VideoGenAPIError,
    VideoGenProductionFailed,
    VideoGenTimeout,
    VideoGenUnavailable,
    VideoGenUnreadableResponse,
)

from ._videogen_stub import (
    RaisingTransport,
    StubTransport,
    client_with,
    export_started_body,
    json_response,
    project_export_body,
    start_workflow_body,
    workflow_run_body,
)


def _succeeding_transport(download_url="https://cdn.videogen.io/vg_file_1.mp4"):
    return StubTransport(
        json_response(200, start_workflow_body()),
        json_response(200, workflow_run_body("succeeded", 100.0)),
        json_response(200, export_started_body()),
        json_response(200, project_export_body("succeeded", 100.0, download_url)),
    )


class ProduceVideoSuccessTests(SimpleTestCase):
    def test_returns_download_url(self):
        transport = _succeeding_transport()
        client = client_with(transport)
        url = videogen_service.produce_video(
            "Tracking Wild Yeast. Yeasts grow as single cells.",
            client=client,
            poll_interval=0,
            total_timeout=30,
        )
        self.assertEqual(url, "https://cdn.videogen.io/vg_file_1.mp4")

    def test_requests_the_cheap_shape_on_the_wire(self):
        transport = _succeeding_transport()
        client = client_with(transport)
        videogen_service.produce_video(
            "A script.", client=client, poll_interval=0, total_timeout=30
        )

        # 4 calls: script_to_video, get_workflow_run, export_project, get_export.
        self.assertEqual(len(transport.requests), 4)

        start = transport.requests[0]
        self.assertEqual(start.method, "POST")
        self.assertTrue(start.url.endswith("/v1/workflows/script-to-video"))
        body = start.body.value
        self.assertEqual(body["script"], "A script.")
        # Stock footage only (no AI imagery), 16:9, and no avatar/remix keys.
        self.assertEqual(body["visualStyle"], {"type": "STOCK"})
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        self.assertNotIn("actorEntityId", body)
        self.assertNotIn("remixActions", body)

        export = transport.requests[2]
        self.assertEqual(export.method, "POST")
        self.assertTrue(export.url.endswith("/v1/projects/vg_proj_1/export"))
        # Exactly one export, at 720p (HIGH), never 4K.
        self.assertEqual(export.body.value, {"quality": "HIGH"})

    def test_polls_workflow_until_terminal(self):
        transport = StubTransport(
            json_response(200, start_workflow_body()),
            json_response(200, workflow_run_body("pending", 0.0)),
            json_response(200, workflow_run_body("running", 40.0)),
            json_response(200, workflow_run_body("succeeded", 100.0)),
            json_response(200, export_started_body()),
            json_response(200, project_export_body("running", 50.0)),
            json_response(
                200, project_export_body("succeeded", 100.0, "https://cdn/v.mp4")
            ),
        )
        client = client_with(transport)
        url = videogen_service.produce_video(
            "A script.", client=client, poll_interval=0, total_timeout=30
        )
        self.assertEqual(url, "https://cdn/v.mp4")
        self.assertEqual(len(transport.requests), 7)


class ProduceVideoFailureTests(SimpleTestCase):
    def test_api_error_becomes_typed(self):
        transport = StubTransport(
            json_response(400, {"message": "bad script", "code": "invalid_request"})
        )
        client = client_with(transport)
        with self.assertRaises(VideoGenAPIError) as ctx:
            videogen_service.produce_video(
                "x", client=client, poll_interval=0, total_timeout=30
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("bad script", ctx.exception.detail)

    def test_failed_job_becomes_production_failed(self):
        transport = StubTransport(
            json_response(200, start_workflow_body()),
            json_response(
                200,
                workflow_run_body(
                    "failed", 30.0, error={"message": "no footage", "code": "not_found"}
                ),
            ),
        )
        client = client_with(transport)
        with self.assertRaises(VideoGenProductionFailed) as ctx:
            videogen_service.produce_video(
                "x", client=client, poll_interval=0, total_timeout=30
            )
        self.assertIn("no footage", str(ctx.exception))

    def test_decode_failure_becomes_unreadable(self):
        # 200 but a body missing every required member -> SDK ValidationError.
        transport = StubTransport(json_response(200, {}))
        client = client_with(transport)
        with self.assertRaises(VideoGenUnreadableResponse):
            videogen_service.produce_video(
                "x", client=client, poll_interval=0, total_timeout=30
            )

    def test_transport_failure_becomes_unavailable(self):
        transport = RaisingTransport(httpx.ConnectError("refused"))
        client = client_with(transport)
        with self.assertRaises(VideoGenUnavailable):
            videogen_service.produce_video(
                "x", client=client, poll_interval=0, total_timeout=30
            )

    def test_export_without_url_is_unreadable(self):
        transport = StubTransport(
            json_response(200, start_workflow_body()),
            json_response(200, workflow_run_body("succeeded", 100.0)),
            json_response(200, export_started_body()),
            # succeeded but no downloadUrl.
            json_response(200, project_export_body("succeeded", 100.0, None)),
        )
        client = client_with(transport)
        with self.assertRaises(VideoGenUnreadableResponse):
            videogen_service.produce_video(
                "x", client=client, poll_interval=0, total_timeout=30
            )

    def test_timeout_when_never_terminal(self):
        transport = StubTransport(
            json_response(200, start_workflow_body()),
            json_response(200, workflow_run_body("running", 10.0)),
        )
        client = client_with(transport)
        with self.assertRaises(VideoGenTimeout):
            videogen_service.produce_video(
                "x", client=client, poll_interval=0, total_timeout=0
            )
