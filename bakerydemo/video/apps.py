from django.apps import AppConfig


class VideoConfig(AppConfig):
    name = "bakerydemo.video"
    label = "video"
    verbose_name = "Article videos"
    default_auto_field = "django.db.models.AutoField"

    def ready(self) -> None:
        # Register the video routes onto the existing Wagtail v3 API instance.
        #
        # Django Ninja requires routers to be added before ``api.urls`` is first
        # accessed. ``ready()`` runs during app population – after every app's
        # models are loaded but well before the URLconf is built – so this is
        # the correct, ordering-safe hook. Registering here (rather than in
        # ``urls.py``) also keeps the integration fully self-contained: dropping
        # the app into ``INSTALLED_APPS`` wires up the endpoints.
        from wagtail.api.v3.urls import api

        from .api import register_video_routes

        register_video_routes(api)
