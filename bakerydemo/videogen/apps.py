from django.apps import AppConfig


class VideogenConfig(AppConfig):
    name = "bakerydemo.videogen"
    label = "videogen"
    default_auto_field = "django.db.models.AutoField"
    verbose_name = "VideoGen integration"

    def ready(self):
        # Register signal handlers (media cleanup).
        # Attach our video routes to the shared Wagtail v3 API instance. This
        # must happen before ``api.urls`` is first accessed (Django Ninja raises
        # ConfigError otherwise); ``ready()`` runs during app loading, well
        # before the URLconf is built, so this is safe. Guard against a second
        # registration if ``ready()`` is somehow invoked twice.
        from wagtail.api.v3.api import api

        from . import signals  # noqa: F401
        from .api import router as video_router

        if not getattr(api, "_bakery_videogen_registered", False):
            api.add_router("/pages/", video_router)
            api._bakery_videogen_registered = True
