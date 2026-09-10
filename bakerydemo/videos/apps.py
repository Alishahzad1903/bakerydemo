from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bakerydemo.videos"
    label = "videos"

    def ready(self):
        # Register the video routes on the existing Wagtail v3 API. Done here so
        # it happens during app loading, before the URLconf accesses api.urls.
        from .api import register_routes

        register_routes()
