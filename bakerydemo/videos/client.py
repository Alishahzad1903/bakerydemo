"""Construction and lifetime of the VideoGen SDK client.

The APIMatic-generated client owns a pooled HTTP transport and must be
long-lived and reused, never rebuilt per request. We hold a single sync client
(Django runs under WSGI) as a lazily-initialised module global, thread-safe to
share, and close it at process exit.
"""

from __future__ import annotations

import atexit
import threading

from django.conf import settings
from videogen import VideogenClient

from .exceptions import VideoGenConfigurationError

_client: VideogenClient | None = None
_lock = threading.Lock()


def get_client() -> VideogenClient:
    """Return the shared sync VideoGen client, building it on first use.

    Raises :class:`VideoGenConfigurationError` if ``VIDEOGEN_API_KEY`` is not
    configured — the SDK would otherwise send unauthenticated requests and only
    fail later with a 401.
    """
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            api_key = settings.VIDEOGEN_API_KEY
            if not api_key:
                raise VideoGenConfigurationError(
                    "VIDEOGEN_API_KEY is not set; cannot talk to VideoGen."
                )
            client = VideogenClient(
                bearer_auth=api_key,
                # None selects the SDK default (https://api.videogen.io); a set
                # VIDEOGEN_BASE_URL is passed verbatim.
                base_url=settings.VIDEOGEN_BASE_URL or None,
                timeout=settings.VIDEOGEN_TIMEOUT,
            )
            atexit.register(client.close)
            _client = client
    return _client
