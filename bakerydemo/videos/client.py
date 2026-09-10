"""Construction and lifetime of the VideoGen SDK client.

The SDK client owns a pooled HTTP transport, so it must be long-lived and
shared rather than rebuilt per request. We hold a single module-level instance,
built lazily on first use (the pragmatic placement for Django under WSGI), and
close it at process exit.

Credentials are read from Django settings at call time — they originate from
the ``VIDEOGEN_API_KEY`` / ``VIDEOGEN_BASE_URL`` environment variables and are
never written into the repository.
"""

from __future__ import annotations

import atexit
import threading

from django.conf import settings
from videogen import VideogenClient

from .exceptions import VideoGenConfigError

_client: VideogenClient | None = None
_lock = threading.Lock()


def _build_client() -> VideogenClient:
    api_key = getattr(settings, "VIDEOGEN_API_KEY", "") or ""
    if not api_key:
        raise VideoGenConfigError(
            "VIDEOGEN_API_KEY is not configured; set it in the environment."
        )

    timeout = getattr(settings, "VIDEOGEN_REQUEST_TIMEOUT_SECONDS", 30.0)

    # base_url is omitted unless explicitly overridden, so the SDK's own
    # default (https://api.videogen.io) is used. When VIDEOGEN_BASE_URL is set
    # it is passed verbatim.
    base_url = getattr(settings, "VIDEOGEN_BASE_URL", None)
    if base_url:
        return VideogenClient(bearer_auth=api_key, timeout=timeout, base_url=base_url)
    return VideogenClient(bearer_auth=api_key, timeout=timeout)


def get_client() -> VideogenClient:
    """Return the shared VideoGen client, building it on first use.

    Raises :class:`VideoGenConfigError` when no API key is configured.
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = _build_client()
                atexit.register(close_client)
    return _client


def close_client() -> None:
    """Close the shared client's HTTP pool, if one was built."""
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None
