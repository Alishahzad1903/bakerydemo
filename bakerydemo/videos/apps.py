from django.apps import AppConfig


class VideosConfig(AppConfig):
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos"

    def ready(self):
        # Registering our Django-Ninja router onto the shared v3 ``api``
        # instance must happen before ``api.urls`` is evaluated in
        # ``bakerydemo.urls`` (Ninja raises ``ConfigError`` otherwise).
        # ``AppConfig.ready`` runs during app population, i.e. before the
        # URLconf is imported, so importing the router module here is the
        # additive way to mount our endpoints without touching the URLconf.
        from . import api  # noqa: F401
