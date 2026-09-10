"""Test doubles for the VideoGen SDK.

The SDK's transport is a structural protocol, so a fake needs no base class and
no ``Mock`` — just ``send`` / ``stream`` / ``close``. Faking the transport (not
the client) exercises the real request-building and decoding pipeline, which is
the whole point: it catches a wrong path, a body that will not serialize, or a
header we forgot. VideoGen uses a plain bearer token (no OAuth), so the first
request the stub sees is the operation itself — there is no token fetch first.
"""

from __future__ import annotations

import json

from videogen import VideogenClient
from videogen.core import HttpRequest, HttpResponse

from bakerydemo.videos.videogen_client import VideoGenService


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},  # lowercase keys, per contract
        content=json.dumps(body).encode(),
    )


class StubTransport:
    """Satisfies the SDK's sync transport protocol; returns queued responses."""

    def __init__(self, *responses: HttpResponse) -> None:
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []
        self.closed = False

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._responses.pop(0)

    def stream(self, request):  # pragma: no cover - nothing under test streams
        raise NotImplementedError("this stub answers send() only")

    def close(self) -> None:
        self.closed = True

    @property
    def last_request(self) -> HttpRequest | None:
        return self.requests[-1] if self.requests else None


class RaisingTransport:
    """A transport whose send() raises, to simulate a transport failure."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        raise self._exc

    def stream(self, request):  # pragma: no cover
        raise self._exc

    def close(self) -> None:
        pass


def service_with(*responses: HttpResponse) -> tuple[VideoGenService, StubTransport]:
    transport = StubTransport(*responses)
    client = VideogenClient(bearer_auth="test-key", custom_http_client=transport)
    return VideoGenService(client), transport


def service_raising(exc: BaseException) -> tuple[VideoGenService, RaisingTransport]:
    transport = RaisingTransport(exc)
    client = VideogenClient(bearer_auth="test-key", custom_http_client=transport)
    return VideoGenService(client), transport
