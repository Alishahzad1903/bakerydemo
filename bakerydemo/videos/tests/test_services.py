import shutil
import tempfile
from unittest import mock

from django.test import TestCase, override_settings

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos.models import VideoJob, VideoJobStatus
from bakerydemo.videos.services import enqueue_video_job, process_video_job
from bakerydemo.videos.videogen.client import ProjectExport, WorkflowRun

from .fakes import FakeVideoGenClient

_MEDIA = tempfile.mkdtemp(prefix="videogen-test-")


@override_settings(MEDIA_ROOT=_MEDIA, VIDEOGEN_RUN_SYNC=True)
class ServiceTestBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        from wagtail.models import Page

        root = Page.objects.get(depth=1)
        home = root.add_child(
            instance=BlogIndexPage(title="Blog", slug="blog-idx")
        )
        self.index = home
        self.article = home.add_child(
            instance=BlogPage(
                title="Tracking Wild Yeast",
                slug="wild-yeast",
                introduction="Yeasts can be contrasted with molds. Ignored tail.",
                live=True,
            )
        )


class EnqueueIdempotencyTests(ServiceTestBase):
    def test_success_then_repeat_returns_same_job_without_rebilling(self):
        fake = FakeVideoGenClient()
        with mock.patch(
            "bakerydemo.videos.services.get_client", return_value=fake
        ):
            job1, created1 = enqueue_video_job(self.article, None)
            self.assertTrue(created1)
            job1.refresh_from_db()
            self.assertEqual(job1.status, VideoJobStatus.READY)
            self.assertEqual(job1.progress_percentage, 100)
            self.assertTrue(job1.video_file)

            # Second request for the same article: same job, no new production.
            job2, created2 = enqueue_video_job(self.article, None)
            self.assertFalse(created2)
            self.assertEqual(job1.id, job2.id)

        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        self.assertEqual(fake.count("create_script_to_video"), 1)

    def test_narration_is_title_plus_first_sentence(self):
        fake = FakeVideoGenClient()
        with mock.patch(
            "bakerydemo.videos.services.get_client", return_value=fake
        ):
            job, _ = enqueue_video_job(self.article, None)
        self.assertEqual(
            job.script, "Tracking Wild Yeast. Yeasts can be contrasted with molds."
        )
        # The script the provider received matches the article's own words.
        create_kwargs = next(
            c[1] for c in fake.calls if c[0] == "create_script_to_video"
        )
        self.assertEqual(create_kwargs["script"], job.script)
        self.assertEqual(create_kwargs["visual_style"], {"type": "STOCK"})
        self.assertEqual(create_kwargs["aspect_ratio"], {"width": 16, "height": 9})


class PipelineTests(ServiceTestBase):
    def test_single_export_at_configured_quality(self):
        fake = FakeVideoGenClient()
        with mock.patch(
            "bakerydemo.videos.services.get_client", return_value=fake
        ):
            enqueue_video_job(self.article, None)
        self.assertEqual(fake.count("export_project"), 1)
        export_call = next(c for c in fake.calls if c[0] == "export_project")
        self.assertEqual(export_call[2]["quality"], "STANDARD")

    def test_workflow_failure_is_recorded(self):
        fake = FakeVideoGenClient(
            workflow_runs=[
                WorkflowRun(
                    "vg_work_fake",
                    "failed",
                    0,
                    error={"message": "stock footage unavailable", "code": "x"},
                )
            ]
        )
        job = VideoJob.objects.create(
            page=self.article, status=VideoJobStatus.PENDING, script="s"
        )
        process_video_job(job.id, client=fake)
        job.refresh_from_db()
        self.assertEqual(job.status, VideoJobStatus.FAILED)
        self.assertIn("stock footage unavailable", job.error)
        # No export attempted when generation failed.
        self.assertEqual(fake.count("export_project"), 0)

    def test_failed_job_allows_a_retry(self):
        VideoJob.objects.create(
            page=self.article, status=VideoJobStatus.FAILED, script="s", error="nope"
        )
        fake = FakeVideoGenClient()
        with mock.patch(
            "bakerydemo.videos.services.get_client", return_value=fake
        ):
            job, created = enqueue_video_job(self.article, None)
        self.assertTrue(created)
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 2)

    def test_export_failure_is_recorded(self):
        fake = FakeVideoGenClient(
            exports=[
                ProjectExport(
                    "vg_export_fake", "vg_proj_fake", "failed", 0,
                    error={"message": "export blew up"},
                )
            ]
        )
        job = VideoJob.objects.create(
            page=self.article, status=VideoJobStatus.PENDING, script="s"
        )
        process_video_job(job.id, client=fake)
        job.refresh_from_db()
        self.assertEqual(job.status, VideoJobStatus.FAILED)
        self.assertIn("export blew up", job.error)
