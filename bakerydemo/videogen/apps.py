from django.apps import AppConfig


class VideoGenConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bakerydemo.videogen"
    label = "videogen"
    verbose_name = "Article video generation"

    def ready(self):
        # Register the page-video endpoints on the shared v3 API router. This
        # must happen before ``api.urls`` is accessed by the root URLconf, which
        # only occurs on the first request - well after app ``ready()`` runs.
        from wagtail.api.v3.urls import api

        from .api import register

        register(api)
