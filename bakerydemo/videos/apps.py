from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos"

    def ready(self):
        # Mount our routes onto the *existing* Wagtail v3 API instance so the
        # feature lives at /api/v3-preview/pages/{page_id}/video/ and shares the
        # v3 API's authentication, error handling and OpenAPI docs.
        #
        # Routers must be registered before ``api.urls`` is first accessed
        # (Django Ninja raises ConfigError otherwise). ``ready()`` runs during
        # ``django.setup()``, before the root URLconf is imported, so this is
        # the correct place to do it.
        from wagtail.api.v3.urls import api

        from .api import register_video_routes

        register_video_routes(api)
