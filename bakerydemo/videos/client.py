"""Construction of the VideoGen SDK client from Django settings.

The API key and optional base-URL override are read from settings (which in turn
read them from the environment). No credential value is ever hard-coded.
"""

from __future__ import annotations

from django.conf import settings
from videogen import VideoGen

from .exceptions import VideoGenConfigurationError


def get_videogen_client() -> VideoGen:
    """Return a configured VideoGen client.

    Raises:
        VideoGenConfigurationError: if ``VIDEOGEN_API_KEY`` is not configured.
    """
    api_key = getattr(settings, "VIDEOGEN_API_KEY", None)
    if not api_key:
        raise VideoGenConfigurationError(
            "VIDEOGEN_API_KEY is not configured; cannot produce article videos."
        )
    # base_url is optional: when VIDEOGEN_BASE_URL is set it is used verbatim,
    # otherwise the SDK falls back to its default base URL.
    base_url = getattr(settings, "VIDEOGEN_BASE_URL", None) or None
    return VideoGen(api_key=api_key, base_url=base_url)
