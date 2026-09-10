from django.apps import AppConfig


class VideoGenConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "bakerydemo.videogen"
    label = "videogen"
    verbose_name = "VideoGen integration"
