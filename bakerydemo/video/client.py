"""
Thin factory around the official VideoGen SDK client.

Credentials are read from Django settings (which read them from the environment
at run time). No credential value is ever hard-coded in this repository.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from videogen import VideoGen

from .exceptions import VideoGenConfigurationError


def build_client() -> VideoGen:
    """
    Return a configured :class:`videogen.VideoGen` client.

    Raises :class:`VideoGenConfigurationError` when no API key is configured so
    a misconfiguration is reported clearly instead of failing deep inside a
    provider call.

    ``VIDEOGEN_BASE_URL`` is an optional override: when set it is passed through
    to the SDK verbatim as the API base address; otherwise the SDK default
    (``https://api.videogen.io``) is used.
    """
    api_key = getattr(settings, "VIDEOGEN_API_KEY", "") or ""
    if not api_key:
        raise VideoGenConfigurationError(
            "VideoGen is not configured: set the VIDEOGEN_API_KEY environment "
            "variable."
        )
    base_url = getattr(settings, "VIDEOGEN_BASE_URL", None) or None
    return VideoGen(api_key=api_key, base_url=base_url)


def get_visual_style() -> dict[str, Any]:
    """
    Return the ``visualStyle`` to request for stock-footage videos.

    VideoGen's script-to-video workflow requires a ``visualStyle`` object. The
    stock-footage value is operator-configured via ``VIDEOGEN_VISUAL_STYLE_TYPE``
    (see the settings comment and the gap note in ``service.py``) so no value is
    ever hard-coded or guessed, and the forbidden AI-image style is never used.

    Raises :class:`VideoGenConfigurationError` when it is not configured.
    """
    style_type = getattr(settings, "VIDEOGEN_VISUAL_STYLE_TYPE", None)
    if not style_type:
        raise VideoGenConfigurationError(
            "No stock-footage visual style is configured. VideoGen's "
            "script-to-video workflow requires a 'visualStyle', but the VideoGen "
            "`api` skill (this integration's sole reference) does not document a "
            "stock-footage visualStyle value - only the AI-image style, which "
            "this site must not use. Set the VIDEOGEN_VISUAL_STYLE_TYPE "
            "environment variable to the stock-footage visual style accepted by "
            "your VideoGen account."
        )
    return {"type": style_type}
