"""Test doubles for the VideoGen SDK.

We fake the SDK's *transport* — the seam the SDK is designed to be tested at —
rather than the client class, so the real request-building and response-decoding
pipeline is exercised. Bearer auth needs no token round-trip, so the stub only
ever sees the operation request itself.
"""

from __future__ import annotations

import json

from videogen import VideogenClient
from videogen.core import HttpRequest, HttpResponse


class StubTransport:
    """Satisfies the SDK's sync transport protocol: send/stream/close."""

    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self._responses: list[HttpResponse | Exception] = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stream(self, request: HttpRequest):
        raise NotImplementedError("this stub answers send() only")

    def close(self) -> None:
        pass

    @property
    def last_request(self) -> HttpRequest | None:
        return self.requests[-1] if self.requests else None


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


def text_response(status: int, text: str) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "text/plain"},
        content=text.encode(),
    )


def make_client(*responses: HttpResponse | Exception) -> tuple[VideogenClient, StubTransport]:
    transport = StubTransport(*responses)
    client = VideogenClient(bearer_auth="test-token", custom_http_client=transport)
    return client, transport
