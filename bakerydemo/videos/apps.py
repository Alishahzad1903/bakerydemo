"""App config for the additive article-video capability.

The Ninja router is registered onto the existing Wagtail v3 API instance in
``ready()``. This runs during ``django.setup()`` — before the URLconf is
imported and therefore before ``api.urls`` is built, which Ninja requires
(it raises ``ConfigError`` if routers are added after URL generation).
"""

from __future__ import annotations

import logging

from django.apps import AppConfig

logger = logging.getLogger("bakerydemo.videos")


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos"

    def ready(self) -> None:
        from ninja.errors import ConfigError
        from wagtail.api.v3.urls import api

        from .api import video_router

        try:
            api.add_router("/pages/", video_router)
        except ConfigError as exc:
            # Already mounted (e.g. ready() invoked twice in one process).
            logger.debug("Video router not re-registered: %s", exc)
