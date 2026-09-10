"""Gateway tests against a faked SDK transport (no network).

The seam is the SDK's transport protocol (``custom_http_client``), so these
exercise the real request-building and response-decoding pipeline — they assert
on the request the SDK actually sends and on the typed exceptions the gateway
raises, not on SDK internals.
"""

from __future__ import annotations

import json

import httpx
from django.test import SimpleTestCase, override_settings
from videogen import VideogenClient
from videogen.core import HttpRequest, HttpResponse

from bakerydemo.video.exceptions import (
    VideoGenAPIError,
    VideoGenConfigError,
    VideoGenResponseError,
    VideoGenTransportError,
)
from bakerydemo.video.provider import VideoGenGateway, build_client


class StubTransport:
    """Satisfies the SDK's sync transport protocol: send/stream/close."""

    def __init__(self, *responses: HttpResponse) -> None:
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._responses.pop(0)

    def stream(self, request: HttpRequest):
        raise NotImplementedError("this stub answers send() only")

    def close(self) -> None:
        pass

    @property
    def last_request(self) -> HttpRequest:
        return self.requests[-1]


class BoomTransport:
    """A transport that fails at the socket, like a dropped connection."""

    def send(self, request: HttpRequest) -> HttpResponse:
        raise httpx.ConnectError("connection refused")

    def stream(self, request: HttpRequest):
        raise httpx.ConnectError("connection refused")

    def close(self) -> None:
        pass


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def gateway_with(transport, *, base_url: str | None = None) -> VideoGenGateway:
    kwargs = {"bearer_auth": "test-token", "custom_http_client": transport}
    if base_url:
        kwargs["base_url"] = base_url
    return VideoGenGateway(client=VideogenClient(**kwargs))


class BuildClientTests(SimpleTestCase):
    @override_settings(VIDEOGEN_API_KEY=None)
    def test_missing_api_key_raises_config_error(self):
        with self.assertRaises(VideoGenConfigError):
            build_client()

    @override_settings(VIDEOGEN_API_KEY="k", VIDEOGEN_BASE_URL=None)
    def test_builds_without_base_url(self):
        client = build_client()
        self.addCleanup(client.close)
        self.assertIsInstance(client, VideogenClient)

    @override_settings(VIDEOGEN_API_KEY="k", VIDEOGEN_BASE_URL="https://vg.example")
    def test_builds_with_base_url_override(self):
        client = build_client()
        self.addCleanup(client.close)
        self.assertIsInstance(client, VideogenClient)


class ScriptToVideoRequestTests(SimpleTestCase):
    def test_sends_cheap_shape_and_returns_ids(self):
        transport = StubTransport(
            json_response(
                200,
                {
                    "workflowRunId": "wf_123",
                    "projectId": "proj_123",
                    "projectUrl": "https://app/x",
                    "remixActionIds": [],
                },
            )
        )
        with gateway_with(transport) as gateway:
            workflow_run_id, project_id = gateway.start_script_to_video(
                "Tracking Wild Yeast. Wild yeast is everywhere"
            )

        self.assertEqual(workflow_run_id, "wf_123")
        self.assertEqual(project_id, "proj_123")

        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v1/workflows/script-to-video"))
        # The cheap shape, asserted on the wire (alias) names.
        payload = req.body.value
        self.assertEqual(
            payload["script"], "Tracking Wild Yeast. Wild yeast is everywhere"
        )
        self.assertEqual(payload["visualStyle"]["type"], "STOCK")
        self.assertEqual(payload["aspectRatio"], {"width": 16, "height": 9})
        # No avatar, no remix actions, no AI imagery requested.
        self.assertNotIn("actorEntityId", payload)
        self.assertNotIn("remixActions", payload)
        self.assertEqual(req.headers["authorization"], "Bearer test-token")

    def test_base_url_override_reaches_the_wire(self):
        transport = StubTransport(
            json_response(
                200,
                {
                    "workflowRunId": "wf",
                    "projectId": "p",
                    "projectUrl": "u",
                    "remixActionIds": [],
                },
            )
        )
        with gateway_with(transport, base_url="https://vg.example") as gateway:
            gateway.start_script_to_video("hello")
        self.assertTrue(transport.last_request.url.startswith("https://vg.example"))


class ExportRequestTests(SimpleTestCase):
    def test_requests_single_720p_export(self):
        transport = StubTransport(json_response(200, {"exportId": "exp_1"}))
        with gateway_with(transport) as gateway:
            export_id = gateway.export_project("proj_123")

        self.assertEqual(export_id, "exp_1")
        req = transport.last_request
        self.assertEqual(req.method, "POST")
        self.assertTrue(req.url.endswith("/v1/projects/proj_123/export"))
        # 720p == HIGH; never ULTRA_HIGH (4K).
        self.assertEqual(req.body.value["quality"], "HIGH")


class ErrorTranslationTests(SimpleTestCase):
    def test_api_error_becomes_typed_api_error(self):
        transport = StubTransport(
            json_response(
                402, {"message": "out of credits", "code": "insufficient_credits"}
            )
        )
        with gateway_with(transport) as gateway:
            with self.assertRaises(VideoGenAPIError) as ctx:
                gateway.export_project("proj_123")

        exc = ctx.exception
        self.assertEqual(exc.status_code, 402)
        self.assertEqual(exc.code, "insufficient_credits")
        self.assertIn("out of credits", str(exc))

    def test_transport_failure_becomes_typed_transport_error(self):
        with gateway_with(BoomTransport()) as gateway:
            with self.assertRaises(VideoGenTransportError):
                gateway.get_workflow_run("wf_123")

    def test_undecodable_success_becomes_response_error(self):
        # 200 with a type mismatch (progressPercentage as a list) — a real decode
        # failure, which the SDK raises rather than returning.
        transport = StubTransport(
            json_response(
                200,
                {
                    "workflowRunId": "wf",
                    "status": "running",
                    "workflowType": "SCRIPT_TO_VIDEO",
                    "progressPercentage": ["not", "a", "number"],
                    "attemptIndex": 0,
                    "projectId": "p",
                    "projectUrl": "u",
                    "error": None,
                },
            )
        )
        with gateway_with(transport) as gateway:
            with self.assertRaises(VideoGenResponseError):
                gateway.get_workflow_run("wf")
