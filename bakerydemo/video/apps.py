from django.apps import AppConfig


class VideoConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.video"
    label = "video"
    verbose_name = "Article videos"
