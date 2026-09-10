from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos"

    def ready(self) -> None:
        # Register the article-video endpoints onto the existing Wagtail v3
        # API instance. This runs during app loading, before the root URLconf
        # (and therefore ``api.urls``) is imported, which is when Django Ninja
        # freezes the router set. Mounting under "/pages/" makes the routes sit
        # at /api/v3-preview/pages/{page_id}/video/... alongside the built-in
        # page endpoints.
        from wagtail.api.v3.api import api

        from .api import router as video_router

        api.add_router("/pages/", video_router)
