"""App config for the article-video capability.

The two video endpoints are attached to Wagtail's existing v3 API here, in
``ready()``, so no core file needs editing and the routes appear under the
already-mounted ``/api/v3-preview/pages/`` prefix. Registration must happen
before ``api.urls`` is first evaluated (Ninja forbids adding routers after
that); ``ready()`` runs during app loading, well before the URLconf is built.
"""

from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    label = "videos"

    def ready(self) -> None:
        from wagtail.api.v3.urls import api

        from .api import router as video_router

        # Mount alongside Wagtail's own pages router at the same prefix; Ninja
        # merges the operations (our paths and operation ids are distinct).
        api.add_router("/pages/", video_router)
