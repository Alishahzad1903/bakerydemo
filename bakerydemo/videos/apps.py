from django.apps import AppConfig


class VideosConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videos"
    label = "videos"
    verbose_name = "Article videos (VideoGen)"

    def ready(self):
        # Mount the video endpoints onto the existing Wagtail v3 API router.
        #
        # ready() runs during django.setup(), before ROOT_URLCONF is resolved
        # (and therefore before ``api.urls`` is first accessed), which is the
        # window Django-Ninja requires for router registration. Registering a
        # distinct Router at the "/pages/" prefix alongside Wagtail's own pages
        # router is supported by Ninja (only re-mounting the *same* router
        # instance needs a url_name_prefix).
        from wagtail.api.v3.api import api

        from .api import router as videos_router

        api.add_router("/pages/", videos_router)
