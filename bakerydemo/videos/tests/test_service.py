from unittest import mock

from django.test import TestCase
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos import service
from bakerydemo.videos.exceptions import VideoGenAPIError
from bakerydemo.videos.models import VideoJob, VideoJobState

from .fakes import FakeVideoGen, project_export, workflow_run


class ServiceTestBase(TestCase):
    def setUp(self):
        root = Page.get_first_root_node()
        self.index = BlogIndexPage(title="Blog", slug="blog-svc")
        root.add_child(instance=self.index)
        self.fake = FakeVideoGen()
        self._slug_counter = 0
        patcher = mock.patch.object(service, "vg", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_page(self, *, live=True, title="Tracking Wild Yeast", intro="Yeasts and molds. More."):
        self._slug_counter += 1
        page = BlogPage(
            title=title,
            introduction=intro,
            slug=f"article-{self._slug_counter}",
            live=live,
        )
        self.index.add_child(instance=page)
        return page


class StartOrGetJobTests(ServiceTestBase):
    def test_creates_job_and_starts_workflow(self):
        page = self.make_page()
        job, created = service.start_or_get_job(page)
        self.assertTrue(created)
        self.assertEqual(job.state, VideoJobState.PRODUCING)
        self.assertEqual(job.workflow_run_id, "wr_test")
        self.assertEqual(job.project_id, "pr_test")
        self.assertEqual(self.fake.calls["start_script_to_video"], 1)

    def test_idempotent_second_call_reuses_job(self):
        page = self.make_page()
        job1, created1 = service.start_or_get_job(page)
        job2, created2 = service.start_or_get_job(page)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(job1.pk, job2.pk)
        # Only ONE workflow ever started (no second video, no second bill).
        self.assertEqual(self.fake.calls["start_script_to_video"], 1)
        self.assertEqual(VideoJob.objects.filter(page=page).count(), 1)

    def test_start_failure_records_error(self):
        page = self.make_page()
        self.fake.script_to_video_result = VideoGenAPIError(400, "nope", "invalid_request")
        job, _ = service.start_or_get_job(page)
        self.assertEqual(job.state, VideoJobState.FAILED)
        self.assertEqual(job.error_code, "invalid_request")


class ReconcileTests(ServiceTestBase):
    def _job(self, **kw):
        page = self.make_page(**kw)
        job, _ = service.start_or_get_job(page)
        return job

    def test_workflow_running_updates_progress(self):
        job = self._job()
        self.fake.workflow_runs = [workflow_run("running", progress=40.0)]
        service.reconcile(job)
        job.refresh_from_db()
        self.assertEqual(job.state, VideoJobState.PRODUCING)
        self.assertEqual(job.progress, 20.0)  # 40 * 0.5

    def test_workflow_succeeded_triggers_single_export(self):
        job = self._job()
        self.fake.workflow_runs = [workflow_run("succeeded", progress=100.0)]
        self.fake.project_exports = [project_export("running", progress=10.0)]
        service.reconcile(job)
        job.refresh_from_db()
        self.assertEqual(job.state, VideoJobState.EXPORTING)
        self.assertEqual(job.export_id, "ex_test")
        self.assertTrue(job.export_requested)
        self.assertEqual(self.fake.calls["start_export"], 1)

    def test_export_claimed_exactly_once(self):
        job = self._job()
        self.fake.workflow_runs = [workflow_run("succeeded")]
        self.fake.project_exports = [project_export("running")]
        service.reconcile(job)
        # A second workflow reconcile must not re-export (state already EXPORTING),
        # and a direct re-claim attempt must be a no-op.
        service._start_export(job)
        self.assertEqual(self.fake.calls["start_export"], 1)

    def test_export_succeeded_downloads_and_becomes_ready(self):
        job = self._job()
        self.fake.workflow_runs = [workflow_run("succeeded")]
        self.fake.project_exports = [
            project_export("running"),
            project_export("succeeded", progress=100.0, download_url="https://cdn/x.mp4"),
        ]
        service.reconcile(job)  # workflow -> export started (running)
        service.reconcile(job)  # export succeeded -> download
        job.refresh_from_db()
        self.assertEqual(job.state, VideoJobState.READY)
        self.assertEqual(job.progress, 100.0)
        self.assertTrue(job.mp4)
        self.assertEqual(self.fake.calls["download_bytes"], 1)

    def test_workflow_failed_marks_failed(self):
        job = self._job()
        self.fake.workflow_runs = [workflow_run("failed", error=None)]
        service.reconcile(job)
        job.refresh_from_db()
        self.assertEqual(job.state, VideoJobState.FAILED)
        self.assertEqual(job.error_code, "workflow_failed")

    def test_api_4xx_during_poll_fails(self):
        job = self._job()
        self.fake.workflow_runs = [VideoGenAPIError(404, "gone", "not_found")]
        service.reconcile(job)
        job.refresh_from_db()
        self.assertEqual(job.state, VideoJobState.FAILED)

    def test_transient_5xx_during_poll_keeps_producing(self):
        job = self._job()
        self.fake.workflow_runs = [VideoGenAPIError(503, "busy", None)]
        service.reconcile(job)
        job.refresh_from_db()
        self.assertEqual(job.state, VideoJobState.PRODUCING)

    def test_reconcile_terminal_is_noop(self):
        job = self._job()
        job.state = VideoJobState.READY
        job.save()
        service.reconcile(job)
        self.assertEqual(self.fake.calls["get_workflow_run"], 0)
