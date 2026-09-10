from django.apps import AppConfig


class VideoConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.video"
    label = "video"
    verbose_name = "Article videos"

    def ready(self):
        # Register the video endpoints on the shared Wagtail v3 API singleton at
        # startup, before its URLs are built on first request.
        from .api import register_video_routes

        register_video_routes()
