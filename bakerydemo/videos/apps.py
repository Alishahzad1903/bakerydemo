from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos"

    def ready(self) -> None:
        # Register the article-video endpoints on Wagtail's shared v3 API
        # instance. This must happen during app loading, before ``api.urls`` is
        # accessed by the URLconf, and keeps the feature fully additive: no
        # change to the project's urls.py or to any existing endpoint.
        from wagtail.api.v3.urls import api

        from .api import router as video_router

        already_registered = any(
            getattr(existing_router, "_bakerydemo_article_videos", False)
            for _prefix, existing_router in api._routers
        )
        if already_registered:
            return

        video_router._bakerydemo_article_videos = True
        api.add_router("/pages/", video_router)
