from django.apps import AppConfig


class VideosAppConfig(AppConfig):
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        # Mount the article-video endpoints onto the existing Wagtail v3 API.
        # This runs during app loading, before the root URLconf is imported and
        # before ``api.urls`` is accessed, which is when Django Ninja freezes its
        # router configuration.
        from . import api  # noqa: F401

        api.register_routes()
