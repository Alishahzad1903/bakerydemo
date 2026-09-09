from django.db import models


class VideoStatus(models.TextChoices):
    """Lifecycle of an :class:`~bakerydemo.videos.models.ArticleVideo`.

    ``PENDING`` and ``PROCESSING`` both mean "still being produced";
    ``READY`` means the MP4 can be downloaded; ``FAILED`` means production
    failed and ``error_message`` explains why.
    """

    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


#: Statuses at which no further VideoGen work is outstanding.
TERMINAL_STATUSES = frozenset({VideoStatus.READY, VideoStatus.FAILED})
