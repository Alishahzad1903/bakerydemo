from django.apps import AppConfig


class VideoConfig(AppConfig):
    """
    Additive "article to video" capability for the Wagtail Bakery.

    This app turns a published blog article into a short, narrated video using
    VideoGen. It is exposed on the existing Wagtail v3 preview HTTP API and does
    not alter any existing page, blog, image or admin behaviour.
    """

    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.video"
    label = "video"
    verbose_name = "Article videos"
