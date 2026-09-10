"""Provider-boundary tests.

These exercise the *real* SDK request-building pipeline through a fake transport
(the SDK's documented test seam), asserting both that the cheap video shape
reaches the wire and that every SDK failure kind is translated into a typed
:mod:`bakerydemo.videos.exceptions` error.
"""

import json

import httpx
from django.test import SimpleTestCase
from videogen import VideogenClient
from videogen.core import HttpRequest, HttpResponse, StreamedResponse

from bakerydemo.videos.exceptions import (
    VideoGenAPIError,
    VideoGenResponseError,
    VideoGenTransportError,
)
from bakerydemo.videos.provider import VideoGenService


class StubTransport:
    """Satisfies the SDK's sync transport protocol (send/stream/close)."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []
        self.raiser = None

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if self.raiser is not None:
            raise self.raiser
        return self._responses.pop(0)

    def stream(self, request: HttpRequest) -> StreamedResponse:
        raise NotImplementedError("stub answers send() only")

    def close(self) -> None:  # pragma: no cover - trivial
        ...

    @property
    def last_request(self) -> HttpRequest:
        return self.requests[-1]


def _json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def _service_with(*responses):
    transport = StubTransport(*responses)
    client = VideogenClient(bearer_auth="test-token", custom_http_client=transport)
    return VideoGenService(client=client), transport


class RequestShapeTests(SimpleTestCase):
    def test_script_to_video_sends_cheap_shape(self):
        service, transport = _service_with(
            _json_response(
                200,
                {
                    "workflowRunId": "vg_work_1",
                    "projectId": "vg_proj_1",
                    "projectUrl": "https://app.videogen.io/p/1",
                    "remixActionIds": [],
                },
            )
        )

        result = service.start_script_to_video(script="Hello world.")

        self.assertEqual(result.workflow_run_id, "vg_work_1")
        self.assertEqual(result.project_id, "vg_proj_1")

        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v1/workflows/script-to-video"), req.url)
        body = req.body.value
        self.assertEqual(body["script"], "Hello world.")
        # Stock footage only, no AI imagery.
        self.assertEqual(body["visualStyle"]["type"], "STOCK")
        self.assertNotIn("aiStyle", body["visualStyle"])
        # 16:9.
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})
        # Voice only — no avatar/presenter, no remix actions.
        self.assertNotIn("actorEntityId", body)
        self.assertNotIn("remixActions", body)
        # Bearer auth reached the wire (lowercase header key).
        self.assertEqual(req.headers["authorization"], "Bearer test-token")

    def test_export_project_requests_720p(self):
        service, transport = _service_with(
            _json_response(200, {"exportId": "vg_expo_1"})
        )

        result = service.export_project("vg_proj_1")

        self.assertEqual(result.export_id, "vg_expo_1")
        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v1/projects/vg_proj_1/export"), req.url)
        # 720p == STANDARD (HIGH renders 1080p); never 4K/ULTRA_HIGH.
        self.assertEqual(req.body.value["quality"], "STANDARD")


class ErrorTranslationTests(SimpleTestCase):
    def test_api_error_becomes_typed_error_with_status_and_code(self):
        service, _ = _service_with(
            _json_response(400, {"message": "bad request", "code": "invalid_request"})
        )
        with self.assertRaises(VideoGenAPIError) as ctx:
            service.start_script_to_video(script="x")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(ctx.exception.message, "bad request")

    def test_auth_failure_is_api_error_401(self):
        service, _ = _service_with(
            _json_response(401, {"message": "missing or invalid API key", "code": "invalid_api_key"})
        )
        with self.assertRaises(VideoGenAPIError) as ctx:
            service.get_workflow_run("vg_work_1")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_undecodable_success_body_is_response_error(self):
        # 200 but missing every required member -> pydantic ValidationError.
        service, _ = _service_with(_json_response(200, {}))
        with self.assertRaises(VideoGenResponseError):
            service.start_script_to_video(script="x")

    def test_transport_failure_is_transport_error(self):
        service, transport = _service_with()
        transport.raiser = httpx.ConnectError("connection refused")
        with self.assertRaises(VideoGenTransportError):
            service.get_project_export("vg_proj_1", "vg_expo_1")
