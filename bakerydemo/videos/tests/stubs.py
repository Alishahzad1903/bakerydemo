"""Test doubles for the VideoGen SDK transport seam.

The SDK client takes a transport in its constructor; passing a stub means no
real network calls happen while the real request-building/decoding pipeline is
exercised. Auth here is a plain bearer string (no OAuth token fetch), so the
first request the stub sees is the operation itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from videogen import VideogenClient
from videogen.core import HttpRequest, HttpResponse


def json_response(status: int, body: object) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode(),
    )


class StubTransport:
    """Satisfies the SDK's sync transport protocol: ``send`` + ``close``.

    Each queued item is either an ``HttpResponse`` returned in order, or a
    callable invoked with the request (e.g. to raise a transport error).
    """

    def __init__(self, *responses: HttpResponse | Callable[[HttpRequest], HttpResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        item = self._responses.pop(0)
        if callable(item):
            return item(request)
        return item

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    @property
    def last_request(self) -> HttpRequest | None:
        return self.requests[-1] if self.requests else None


def build_client(*responses) -> tuple[VideogenClient, StubTransport]:
    transport = StubTransport(*responses)
    client = VideogenClient(
        custom_http_client=transport,
        bearer_auth="test-key",
        base_url="https://api.videogen.io",
        timeout=30.0,
    )
    return client, transport
