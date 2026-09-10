from django.apps import AppConfig


class VideoConfig(AppConfig):
    """
    Additive "turn a published article into a shareable video" capability.

    The endpoints live on the site's existing Wagtail v3 HTTP API. Django Ninja
    requires every router to be registered on the shared ``api`` instance before
    ``api.urls`` is first accessed, so we register ours here in ``ready()`` —
    which runs during ``django.setup()``, before the URLconf is imported.
    """

    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.video"
    label = "video"
    verbose_name = "Article videos"

    def ready(self):
        from wagtail.api.v3.urls import api

        from .api import router as video_router

        # Guard against double registration (e.g. autoreload re-running ready()).
        if getattr(api, "_bakerydemo_video_router_registered", False):
            return
        api.add_router("/", video_router)
        api._bakerydemo_video_router_registered = True
