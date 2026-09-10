from unittest import mock

from django.test import TestCase
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videogen import service
from bakerydemo.videogen.exceptions import VideoGenProductionError
from bakerydemo.videogen.models import JobStatus, VideoJob
from bakerydemo.videogen.provider import FinishedExport, StartedRun


class FakeProvider:
    """Stands in for VideoGenProvider; records calls, writes fake bytes."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.closed = False
        FakeProvider.instances.append(self)

    def start_script_to_video(self, script):
        self.calls.append(("start", script))
        return StartedRun(workflow_run_id="vg_work_1", project_id="vg_proj_1")

    def wait_for_run(self, run_id, on_progress=None):
        self.calls.append(("wait", run_id))
        if on_progress:
            on_progress(50.0)
        return {"status": "succeeded"}

    def export_video(self, project_id):
        self.calls.append(("export", project_id))
        return FinishedExport(
            export_id="vg_exp_1",
            download_url="https://cdn.example/v.mp4",
            export_file_id="vg_file_1",
        )

    def download_to(self, url, fileobj):
        self.calls.append(("download", url))
        fileobj.write(b"FAKE-MP4-CONTENT")
        return len(b"FAKE-MP4-CONTENT")

    def close(self):
        self.closed = True


class ServiceTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        cls.index = BlogIndexPage(title="Blog", slug="blog-svc")
        root.add_child(instance=cls.index)
        cls.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="twy-svc",
            introduction="Yeasts grow as single cells. Ignored second sentence.",
            live=True,
        )
        cls.index.add_child(instance=cls.article)

    def setUp(self):
        FakeProvider.instances = []


class RequestVideoTests(ServiceTestBase):
    def test_request_is_idempotent_per_article(self):
        with mock.patch.object(service, "_spawn") as spawn:
            job1, created1 = service.request_video(self.article)
            job2, created2 = service.request_video(self.article)

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(job1.pk, job2.pk)
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        # Worker scheduled once (for the first, real request only).
        self.assertEqual(spawn.call_count, 0)  # on_commit not fired in TestCase

    def test_request_stores_short_script(self):
        with mock.patch.object(service, "_spawn"):
            job, _ = service.request_video(self.article)
        self.assertIn("Tracking Wild Yeast", job.script)
        self.assertNotIn("Ignored second sentence", job.script)
        self.assertLessEqual(len(job.script.split()), 30)

    def test_failed_job_allows_a_new_request(self):
        with mock.patch.object(service, "_spawn"):
            job1, _ = service.request_video(self.article)
            job1.mark_failed("nope")
            job2, created2 = service.request_video(self.article)
        self.assertTrue(created2)
        self.assertNotEqual(job1.pk, job2.pk)


class RunJobTests(ServiceTestBase):
    def test_run_job_success(self):
        with mock.patch.object(service, "_spawn"):
            job, _ = service.request_video(self.article)
        with mock.patch.object(service, "VideoGenProvider", FakeProvider):
            service.run_job(job.pk)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.SUCCEEDED)
        self.assertEqual(job.api_status, "ready")
        self.assertEqual(job.progress_percentage, 100)
        self.assertTrue(job.has_video)
        self.assertEqual(job.video_file.read(), b"FAKE-MP4-CONTENT")
        self.assertEqual(job.provider_workflow_run_id, "vg_work_1")
        self.assertEqual(job.provider_project_id, "vg_proj_1")
        self.assertEqual(job.provider_export_id, "vg_exp_1")
        # Provider was used and closed; export happened exactly once.
        provider = FakeProvider.instances[0]
        self.assertTrue(provider.closed)
        self.assertEqual([c[0] for c in provider.calls].count("export"), 1)
        self.assertEqual([c[0] for c in provider.calls].count("start"), 1)

    def test_run_job_records_provider_failure(self):
        with mock.patch.object(service, "_spawn"):
            job, _ = service.request_video(self.article)

        class FailingProvider(FakeProvider):
            def wait_for_run(self, run_id, on_progress=None):
                raise VideoGenProductionError("render failed")

        with mock.patch.object(service, "VideoGenProvider", FailingProvider):
            service.run_job(job.pk)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.api_status, "failed")
        self.assertIn("render failed", job.error)
        self.assertFalse(job.has_video)
        self.assertTrue(FakeProvider.instances[0].closed)
