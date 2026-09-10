"""Signal handlers for the videogen app."""

from __future__ import annotations

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import VideoJob


@receiver(post_delete, sender=VideoJob)
def delete_video_file(sender, instance: VideoJob, **kwargs) -> None:
    """Remove the stored MP4 when its job is deleted.

    A job is deleted when its article is deleted (``on_delete=CASCADE``), so
    this keeps orphaned video files from lingering in media storage.
    """
    if instance.video_file:
        instance.video_file.delete(save=False)
