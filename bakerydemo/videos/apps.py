from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    verbose_name = "Article videos"

    def ready(self) -> None:
        # Register our two operations onto the shared Wagtail v3 API instance,
        # under the existing "/pages/" prefix, before its URLs are first accessed.
        # This is additive: it does not touch the core pages/sites/schema routers.
        from wagtail.api.v3.api import api

        from .api import video_router

        api.add_router("/pages/", video_router)
