from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos"

    def ready(self):
        # Register the article-video routes on the shared v3-preview Ninja API
        # instance. This must happen before ``bakerydemo.urls`` accesses
        # ``api.urls`` at URLconf-load time; ``ready()`` runs during
        # ``django.setup()``, which is earlier, so the routes are attached in
        # time. Kept idempotent so repeated app loading (e.g. in tests) is safe.
        from . import api

        api.register_routes()
