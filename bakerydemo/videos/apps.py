from django.apps import AppConfig


class VideosConfig(AppConfig):
    name = "bakerydemo.videos"
    label = "videos"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        # Register the video router (and VideoGen error handlers) on the shared
        # Wagtail v3 API during startup, before the URLconf accesses api.urls.
        from .api import register

        register()
