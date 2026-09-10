"""Orchestration tests — idempotency and the build→export→ready state machine.

The VideoGen provider is faked here (no network, no billing); the SDK request
pipeline itself is covered in ``test_provider``.
"""

from types import SimpleNamespace

from django.test import TestCase
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos import service
from bakerydemo.videos.models import ArticleVideo


class FakeService:
    """Stand-in for VideoGenService returning canned, terminal results."""

    def __init__(
        self,
        *,
        workflow_status="succeeded",
        export_status="succeeded",
        workflow_error=None,
        export_error=None,
        download_url="https://videogen.example/out.mp4",
    ):
        self.workflow_status = workflow_status
        self.export_status = export_status
        self.workflow_error = workflow_error
        self.export_error = export_error
        self.download_url = download_url
        self.calls = []

    def start_script_to_video(self, *, script):
        self.calls.append(("start", script))
        return SimpleNamespace(workflow_run_id="vg_work_1", project_id="vg_proj_1")

    def get_workflow_run(self, workflow_run_id):
        self.calls.append(("get_workflow", workflow_run_id))
        return SimpleNamespace(
            status=self.workflow_status,
            progress_percentage=100.0,
            error=self.workflow_error,
        )

    def export_project(self, project_id):
        self.calls.append(("export", project_id))
        return SimpleNamespace(export_id="vg_expo_1")

    def get_project_export(self, project_id, export_id):
        self.calls.append(("get_export", project_id, export_id))
        return SimpleNamespace(
            status=self.export_status,
            progress_percentage=100.0,
            download_url=self.download_url,
            error=self.export_error,
        )


class VideoServiceTestBase(TestCase):
    def setUp(self):
        root = Page.objects.filter(depth=1).first()
        self.index = BlogIndexPage(title="Blog", slug="blog-test")
        root.add_child(instance=self.index)
        self.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="tracking-wild-yeast-test",
            introduction="Yeasts are fascinating. They do many things.",
            live=True,
        )
        self.index.add_child(instance=self.article)


class IdempotencyTests(VideoServiceTestBase):
    def test_second_request_reuses_job_and_does_not_relaunch(self):
        launched = []

        def launcher(pk):
            launched.append(pk)

        with self.captureOnCommitCallbacks(execute=True):
            first = service.request_video(self.article, launcher=launcher)
        with self.captureOnCommitCallbacks(execute=True):
            second = service.request_video(self.article, launcher=launcher)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(len(launched), 1)
        self.assertEqual(ArticleVideo.objects.filter(page=self.article).count(), 1)

    def test_script_is_built_from_article_words(self):
        with self.captureOnCommitCallbacks(execute=True):
            av = service.request_video(self.article, launcher=lambda pk: None)
        self.assertIn("Tracking Wild Yeast", av.script)
        self.assertLessEqual(len(av.script.split()), 30)

    def test_failed_job_can_be_restarted_with_a_new_job_id(self):
        launched = []
        with self.captureOnCommitCallbacks(execute=True):
            first = service.request_video(self.article, launcher=launched.append)
        old_job_id = first.job_id
        ArticleVideo.objects.filter(pk=first.pk).update(
            status=ArticleVideo.Status.FAILED
        )

        with self.captureOnCommitCallbacks(execute=True):
            restarted = service.request_video(self.article, launcher=launched.append)

        self.assertEqual(restarted.pk, first.pk)
        self.assertNotEqual(restarted.job_id, old_job_id)
        self.assertEqual(restarted.status, ArticleVideo.Status.PROCESSING)
        self.assertEqual(len(launched), 2)


class RunJobTests(VideoServiceTestBase):
    def _make_job(self):
        return ArticleVideo.objects.create(
            page=self.article,
            script="Tracking Wild Yeast. Yeasts are fascinating.",
            status=ArticleVideo.Status.PROCESSING,
        )

    def test_happy_path_reaches_ready_with_download_url(self):
        job = self._make_job()
        fake = FakeService()

        service.run_job(job.pk, service=fake)

        job.refresh_from_db()
        self.assertEqual(job.status, ArticleVideo.Status.READY)
        self.assertEqual(job.download_url, "https://videogen.example/out.mp4")
        self.assertEqual(job.progress_percentage, 100.0)
        self.assertEqual(job.workflow_run_id, "vg_work_1")
        self.assertEqual(job.export_id, "vg_expo_1")
        # Each billed step ran exactly once.
        self.assertEqual(len([c for c in fake.calls if c[0] == "start"]), 1)
        self.assertEqual(len([c for c in fake.calls if c[0] == "export"]), 1)

    def test_workflow_failure_marks_failed_and_skips_export(self):
        job = self._make_job()
        fake = FakeService(
            workflow_status="failed",
            workflow_error=SimpleNamespace(message="boom", code="bad_input"),
        )

        service.run_job(job.pk, service=fake)

        job.refresh_from_db()
        self.assertEqual(job.status, ArticleVideo.Status.FAILED)
        self.assertIn("boom", job.error)
        # Export must never be attempted after a build failure (no extra billing).
        self.assertEqual([c for c in fake.calls if c[0] == "export"], [])

    def test_provider_error_marks_failed(self):
        job = self._make_job()

        class Boom:
            def start_script_to_video(self, *, script):
                from bakerydemo.videos.exceptions import VideoGenAPIError

                raise VideoGenAPIError(402, "out of credits", "insufficient_credits")

        service.run_job(job.pk, service=Boom())

        job.refresh_from_db()
        self.assertEqual(job.status, ArticleVideo.Status.FAILED)
        self.assertIn("out of credits", job.error)

    def test_fresh_download_url_none_while_processing(self):
        job = self._make_job()
        self.assertIsNone(service.fresh_download_url(job))
