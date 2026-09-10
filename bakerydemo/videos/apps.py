from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos"

    def ready(self):
        # Additively mount the video endpoints onto the existing Wagtail v3 API.
        # This runs during app loading, before ``bakerydemo/urls.py`` first accesses
        # ``api.urls`` (Django Ninja requires routers to be registered by then).
        from .api import register_video_router

        register_video_router()
