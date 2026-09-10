"""Construction and lifetime of the sync VideoGen SDK client.

The site runs under WSGI (sync Django), so we use the sync ``VideogenClient`` and
hold a single long-lived instance (it owns an ``httpx`` connection pool and a
per-client credential — never build one per request). Credentials and the
optional base-URL override are read from Django settings, which read them from
the environment; no secret value is ever hard-coded here.
"""

from __future__ import annotations

import atexit
import threading

from django.conf import settings
from videogen import VideogenClient

from .exceptions import VideoGenConfigError

_lock = threading.Lock()
_client: VideogenClient | None = None
_client_key: tuple[str, str | None] | None = None


def get_client() -> VideogenClient:
    """Return the process-wide VideoGen client, building it on first use.

    Rebuilds if the configured credentials/base URL change (e.g. in tests). Raises
    ``VideoGenConfigError`` when no API key is configured, so a missing credential
    fails loudly at our boundary rather than as a silent unauthenticated request.
    """
    global _client, _client_key

    api_key = getattr(settings, "VIDEOGEN_API_KEY", None)
    if not api_key:
        raise VideoGenConfigError(
            "VIDEOGEN_API_KEY is not configured; cannot talk to VideoGen."
        )
    base_url: str | None = getattr(settings, "VIDEOGEN_BASE_URL", None) or None
    timeout: float = float(getattr(settings, "VIDEOGEN_TIMEOUT", 30.0))
    key = (api_key, base_url)

    with _lock:
        if _client is None or _client_key != key:
            if _client is not None:
                _client.close()
            # base_url is passed verbatim only when set; otherwise the SDK default
            # (https://api.videogen.io) applies.
            if base_url:
                _client = VideogenClient(
                    bearer_auth=api_key, base_url=base_url, timeout=timeout
                )
            else:
                _client = VideogenClient(bearer_auth=api_key, timeout=timeout)
            _client_key = key
        return _client


@atexit.register
def _close_client() -> None:
    global _client, _client_key
    with _lock:
        if _client is not None:
            _client.close()
            _client = None
            _client_key = None
