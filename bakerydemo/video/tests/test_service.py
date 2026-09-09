from unittest import mock

from django.test import TestCase

from bakerydemo.video.exceptions import VideoGenAuthError
from bakerydemo.video.models import VideoJob
from bakerydemo.video.service import parse_aspect_ratio, run_job, start_or_get_job

from .fakes import FakeVideoGenClient
from .helpers import create_blog_article


class ParseAspectRatioTests(TestCase):
    def test_parses_string_ratio(self):
        self.assertEqual(parse_aspect_ratio("16:9"), {"width": 16, "height": 9})

    def test_passes_through_object(self):
        obj = {"width": 9, "height": 16}
        self.assertEqual(parse_aspect_ratio(obj), obj)

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_aspect_ratio(""))
        self.assertIsNone(parse_aspect_ratio("not-a-ratio"))


class StartOrGetJobTests(TestCase):
    def test_reuses_active_job(self):
        article = create_blog_article()
        job1, created1 = start_or_get_job(article)
        job2, created2 = start_or_get_job(article)

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(job1.pk, job2.pk)
        self.assertEqual(VideoJob.objects.filter(page=article).count(), 1)

    def test_new_job_allowed_after_failure(self):
        article = create_blog_article()
        job1, _ = start_or_get_job(article)
        job1.mark_failed("nope")

        job2, created2 = start_or_get_job(article)
        self.assertTrue(created2)
        self.assertNotEqual(job1.pk, job2.pk)


class RunJobTests(TestCase):
    def test_success_pipeline_produces_download_url(self):
        article = create_blog_article()
        job, _ = start_or_get_job(article)
        fake = FakeVideoGenClient()

        run_job(job.pk, client=fake)

        job.refresh_from_db()
        self.assertEqual(job.status, VideoJob.Status.READY)
        self.assertEqual(job.progress, 100)
        self.assertEqual(job.download_url, fake.download_url)
        self.assertEqual(job.workflow_run_id, "vg_work_test")
        self.assertEqual(job.project_id, "vg_proj_test")
        self.assertEqual(job.export_id, "vg_exp_test")
        # Voice-only, stock footage, 16:9 object aspect ratio.
        self.assertEqual(fake.script_calls[0]["visual_style"], {"type": "STOCK"})
        self.assertEqual(
            fake.script_calls[0]["aspect_ratio"], {"width": 16, "height": 9}
        )
        self.assertEqual(len(fake.script_calls), 1)

    def test_provider_failure_marks_job_failed(self):
        article = create_blog_article()
        job, _ = start_or_get_job(article)

        client = mock.Mock()
        client.create_script_to_video.side_effect = VideoGenAuthError(
            "bad key", status_code=401
        )

        run_job(job.pk, client=client)

        job.refresh_from_db()
        self.assertEqual(job.status, VideoJob.Status.FAILED)
        self.assertIn("bad key", job.error)

    def test_terminal_failed_run_is_reported(self):
        article = create_blog_article()
        job, _ = start_or_get_job(article)

        client = mock.Mock()
        client.create_script_to_video.return_value = {
            "workflowRunId": "vg_work_1",
            "projectId": "vg_proj_1",
        }
        client.get_workflow_run.return_value = {
            "status": "failed",
            "progressPercentage": 40,
            "error": {"message": "render exploded"},
        }

        run_job(job.pk, client=client)

        job.refresh_from_db()
        self.assertEqual(job.status, VideoJob.Status.FAILED)
        self.assertIn("render exploded", job.error)

    def test_already_processing_job_is_not_rerun(self):
        article = create_blog_article()
        job, _ = start_or_get_job(article)
        job.mark_processing()
        client = mock.Mock()

        run_job(job.pk, client=client)

        client.create_script_to_video.assert_not_called()

    def test_export_without_download_url_fails(self):
        article = create_blog_article()
        job, _ = start_or_get_job(article)
        client = mock.Mock()
        client.create_script_to_video.return_value = {
            "workflowRunId": "w",
            "projectId": "p",
        }
        client.get_workflow_run.return_value = {"status": "succeeded", "projectId": "p"}
        client.create_export.return_value = {"exportId": "e"}
        client.get_export.return_value = {"status": "succeeded", "downloadUrl": ""}

        run_job(job.pk, client=client)

        job.refresh_from_db()
        self.assertEqual(job.status, VideoJob.Status.FAILED)


class RefreshDownloadUrlTests(TestCase):
    def test_refresh_when_expired(self):
        from datetime import timedelta

        from django.utils import timezone

        from bakerydemo.video.service import maybe_refresh_download_url

        article = create_blog_article()
        job, _ = start_or_get_job(article)
        job.status = VideoJob.Status.READY
        job.project_id = "vg_proj_test"
        job.export_id = "vg_exp_test"
        job.download_url = "https://old.example/expired.mp4"
        job.download_url_expires_at = timezone.now() - timedelta(hours=1)
        job.save()

        fake = FakeVideoGenClient(download_url="https://fresh.example/new.mp4")
        maybe_refresh_download_url(job, client=fake)

        job.refresh_from_db()
        self.assertEqual(job.download_url, "https://fresh.example/new.mp4")
