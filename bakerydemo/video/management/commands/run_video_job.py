"""
Run a pending video job synchronously.

Useful for operations (e.g. a cron-driven worker) and for verifying the
integration without relying on the background thread. NOTE: running a pending
job produces a real, billed VideoGen video.
"""

from django.core.management.base import BaseCommand, CommandError

from bakerydemo.video.models import VideoJob
from bakerydemo.video.service import run_job


class Command(BaseCommand):
    help = "Run a pending VideoGen job to completion (produces a real, billed video)."

    def add_arguments(self, parser):
        parser.add_argument("job_id", help="UUID of the VideoJob to run")

    def handle(self, *args, **options):
        job_id = options["job_id"]
        try:
            job = VideoJob.objects.get(pk=job_id)
        except (VideoJob.DoesNotExist, ValueError) as exc:
            raise CommandError(f"No video job with id {job_id!r}") from exc

        self.stdout.write(
            f"Running job {job.pk} for page {job.page_id} (status={job.status})..."
        )
        run_job(job.pk)

        job.refresh_from_db()
        self.stdout.write(f"Finished: status={job.status} progress={job.progress}")
        if job.error:
            self.stdout.write(self.style.ERROR(f"error={job.error}"))
        if job.download_url:
            self.stdout.write(self.style.SUCCESS(f"downloadUrl={job.download_url}"))
