import tempfile

from django.test import TestCase, override_settings
from wagtail.models import Page

from bakerydemo.video.exceptions import VideoGenBadRequestError
from bakerydemo.video.models import VideoJob, VideoJobStatus
from bakerydemo.video.service import run_pipeline, start_video_job

from .fakes import FakeVideoGenClient


def _make_page(title="Tracking Wild Yeast", live=True):
    root = Page.get_first_root_node()
    page = Page(title=title, slug=title.lower().replace(" ", "-"), live=live)
    root.add_child(instance=page)
    return page


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class RunPipelineTests(TestCase):
    def setUp(self):
        self.page = _make_page()

    def _run(self, fake):
        job, started = start_video_job(
            self.page,
            runner=lambda jid: run_pipeline(jid, client=fake, poll_interval=0),
        )
        self.assertTrue(started)
        job.refresh_from_db()
        return job

    def test_happy_path_produces_exactly_one_video(self):
        fake = FakeVideoGenClient(workflow_polls_running=1, export_polls_running=1)
        job = self._run(fake)

        self.assertEqual(job.status, VideoJobStatus.READY)
        self.assertEqual(job.progress_percentage, 100)
        self.assertTrue(job.video_file)
        self.assertEqual(job.video_file.read(), b"FAKE-MP4-BYTES")
        self.assertEqual(job.error, "")
        # Cost-safety: one workflow, one export, one download. No more.
        self.assertEqual(fake.create_count, 1)
        self.assertEqual(fake.export_count, 1)
        self.assertEqual(fake.calls.count("download"), 1)
        # 720p / stock-only / 16:9 shape reached the provider.
        self.assertEqual(fake.export_quality, "STANDARD")
        self.assertEqual(fake.create_kwargs["visual_style_type"], "STOCK")
        self.assertEqual(fake.create_kwargs["aspect_ratio"], "16:9")

    def test_workflow_failure_is_recorded_and_skips_export(self):
        fake = FakeVideoGenClient(fail_workflow=True)
        job = self._run(fake)

        self.assertEqual(job.status, VideoJobStatus.FAILED)
        self.assertIn("stock footage lookup failed", job.error)
        self.assertEqual(fake.export_count, 0)
        self.assertFalse(job.video_file)

    def test_export_failure_is_recorded(self):
        fake = FakeVideoGenClient(fail_export=True)
        job = self._run(fake)

        self.assertEqual(job.status, VideoJobStatus.FAILED)
        self.assertIn("render error", job.error)

    def test_provider_exception_marks_job_failed(self):
        fake = FakeVideoGenClient(
            raise_on_create=VideoGenBadRequestError("script too long", status=400)
        )
        job = self._run(fake)

        self.assertEqual(job.status, VideoJobStatus.FAILED)
        self.assertIn("script too long", job.error)

    def test_success_without_download_url_fails(self):
        fake = FakeVideoGenClient(succeed_without_download_url=True)
        job = self._run(fake)

        self.assertEqual(job.status, VideoJobStatus.FAILED)
        self.assertIn("download URL", job.error)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class IdempotencyTests(TestCase):
    def setUp(self):
        self.page = _make_page()

    def test_second_request_returns_same_job_without_reproducing(self):
        started_ids = []
        runner = started_ids.append

        job1, started1 = start_video_job(self.page, runner=runner)
        job2, started2 = start_video_job(self.page, runner=runner)

        self.assertTrue(started1)
        self.assertFalse(started2)
        self.assertEqual(job1.id, job2.id)
        self.assertEqual(VideoJob.objects.count(), 1)
        # The production runner ran only once.
        self.assertEqual(len(started_ids), 1)

    def test_ready_job_is_returned_untouched(self):
        fake = FakeVideoGenClient()
        job, _ = start_video_job(
            self.page,
            runner=lambda jid: run_pipeline(jid, client=fake, poll_interval=0),
        )
        job.refresh_from_db()
        self.assertEqual(job.status, VideoJobStatus.READY)

        # A repeat request must not create a second client/video.
        fake2 = FakeVideoGenClient()
        job2, started = start_video_job(
            self.page,
            runner=lambda jid: run_pipeline(jid, client=fake2, poll_interval=0),
        )
        self.assertFalse(started)
        self.assertEqual(job.id, job2.id)
        self.assertEqual(fake2.create_count, 0)

    def test_failed_job_is_retried_in_place(self):
        job, _ = start_video_job(self.page, runner=lambda jid: None)
        job.mark_failed("boom")

        job2, started = start_video_job(self.page, runner=lambda jid: None)
        self.assertTrue(started)
        self.assertEqual(job.id, job2.id)  # same row reused
        self.assertEqual(job2.status, VideoJobStatus.PENDING)
        self.assertEqual(job2.error, "")
        self.assertEqual(VideoJob.objects.count(), 1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ResumeTests(TestCase):
    """A retry after the workflow already produced the project must re-export
    that project, never re-run the workflow (which would bill a 2nd video)."""

    def setUp(self):
        self.page = _make_page()

    def test_reset_preserves_completed_workflow_project(self):
        job, _ = start_video_job(self.page, runner=lambda jid: None)
        job.project_id = "vg_proj_existing"
        job.workflow_run_id = "vg_work_existing"
        job.export_id = "vg_export_old"
        job.save()
        job.mark_failed("export blew up")

        job.reset_for_retry("new script")
        job.refresh_from_db()
        # Workflow artifacts kept; export side cleared.
        self.assertEqual(job.project_id, "vg_proj_existing")
        self.assertEqual(job.workflow_run_id, "vg_work_existing")
        self.assertEqual(job.export_id, "")
        self.assertEqual(job.status, VideoJobStatus.PENDING)

    def test_run_pipeline_skips_workflow_when_project_exists(self):
        job, _ = start_video_job(self.page, runner=lambda jid: None)
        job.project_id = "vg_proj_existing"
        job.workflow_run_id = "vg_work_existing"
        job.save()

        fake = FakeVideoGenClient()
        run_pipeline(str(job.id), client=fake, poll_interval=0)
        job.refresh_from_db()

        self.assertEqual(job.status, VideoJobStatus.READY)
        # The workflow was NOT re-run; only the export happened.
        self.assertEqual(fake.create_count, 0)
        self.assertEqual(fake.export_count, 1)
