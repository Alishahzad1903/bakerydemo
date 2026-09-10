from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos"

    def ready(self):
        # Register the v3 API routes on the shared NinjaAPI instance. This must
        # happen before ``api.urls`` is first accessed (Django Ninja freezes its
        # routers at that point); ``ready()`` runs during app population, which
        # is before the root URLconf is imported, so the ordering holds.
        from bakerydemo.videos.api import register_video_routes

        register_video_routes()
