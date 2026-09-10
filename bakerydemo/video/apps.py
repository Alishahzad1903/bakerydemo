from django.apps import AppConfig


class VideoConfig(AppConfig):
    name = "bakerydemo.video"
    label = "video"
    default_auto_field = "django.db.models.AutoField"
    verbose_name = "Article videos"

    def ready(self) -> None:
        # Register the video endpoints on the v3 API singleton before its URLs
        # are built. ``ready()`` runs during app loading, ahead of URLconf import.
        from .api import register

        register()
