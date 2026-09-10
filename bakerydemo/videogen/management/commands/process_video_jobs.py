"""Advance any non-terminal video jobs synchronously.

Video production normally runs in a background thread started by the API. This
command is a resumable fallback: if the process restarted while a job was mid
flight, run it to pick the job up again. Because every VideoGen identifier is
persisted on the job as it is obtained, resuming never creates a second video or
a second export — it continues from wherever the job got to.

Usage::

    python manage.py process_video_jobs            # advance all active jobs
    python manage.py process_video_jobs --job <uuid>
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from ...models import ACTIVE_STATUSES, VideoJob
from ...services import run_video_job


class Command(BaseCommand):
    help = "Advance pending/processing video jobs to completion (resumable)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--job",
            dest="job_uuid",
            default=None,
            help="Only process the job with this uuid (videoJobId).",
        )

    def handle(self, *args, **options):
        job_uuid = options["job_uuid"]
        if job_uuid:
            try:
                jobs = [VideoJob.objects.get(uuid=job_uuid)]
            except VideoJob.DoesNotExist as exc:
                raise CommandError(f"No video job with uuid {job_uuid}") from exc
        else:
            jobs = list(VideoJob.objects.filter(status__in=ACTIVE_STATUSES))

        if not jobs:
            self.stdout.write("No video jobs to process.")
            return

        for job in jobs:
            self.stdout.write(f"Processing {job.uuid} (page {job.page_id})...")
            result = run_video_job(job.pk)
            self.stdout.write(f"  -> {result.status}")
            if result.status == "failed":
                self.stderr.write(f"  error: {result.error}")
