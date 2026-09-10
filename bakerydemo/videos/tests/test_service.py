from unittest import mock

from django.test import TestCase
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos import service
from bakerydemo.videos.models import VideoJob
from bakerydemo.videos.videogen.exceptions import VideoGenAuthenticationError

from .fakes import FakeVideoGenClient


def make_blog_page(title="Tracking Wild Yeast", introduction="Yeasts are fungi. More."):
    root = Page.objects.get(depth=1)
    index = root.add_child(instance=BlogIndexPage(title="Blog", slug="blog-test"))
    return index.add_child(
        instance=BlogPage(title=title, slug="twy-test", introduction=introduction)
    )


class RunPipelineTests(TestCase):
    def setUp(self):
        self.page = make_blog_page()
        self.job = VideoJob.objects.create(
            page=self.page,
            status=VideoJob.Status.PENDING,
            script="Tracking Wild Yeast. Yeasts are fungi.",
        )

    def test_happy_path_reaches_ready_with_single_create_and_export(self):
        client = FakeVideoGenClient()
        service.run_pipeline(self.job.id, client=client)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoJob.Status.READY)
        self.assertEqual(self.job.progress_percentage, 100)
        self.assertEqual(
            self.job.download_url, "https://videogen.example/download/final.mp4"
        )
        self.assertEqual(self.job.error, "")
        # Spend guarantees: exactly one billable workflow and one export.
        self.assertEqual(len(client.create_calls), 1)
        self.assertEqual(len(client.export_calls), 1)
        # Cheap shape assertions.
        self.assertEqual(client.create_calls[0]["visual_style"], {"type": "STOCK"})
        self.assertEqual(
            client.create_calls[0]["aspect_ratio"], {"width": 16, "height": 9}
        )
        self.assertEqual(client.export_calls[0]["quality"], "STANDARD")

    def test_failed_run_marks_failed_and_never_exports(self):
        client = FakeVideoGenClient(
            run_status="failed", run_error={"message": "no footage", "code": "x"}
        )
        service.run_pipeline(self.job.id, client=client)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoJob.Status.FAILED)
        self.assertIn("no footage", self.job.error)
        self.assertEqual(len(client.export_calls), 0)

    def test_failed_export_marks_failed(self):
        client = FakeVideoGenClient(
            export_status="failed", export_error={"message": "render broke"}
        )
        service.run_pipeline(self.job.id, client=client)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoJob.Status.FAILED)
        self.assertIn("render broke", self.job.error)

    def test_provider_exception_is_recorded_as_error(self):
        client = FakeVideoGenClient(
            create_error=VideoGenAuthenticationError("bad key", status=401)
        )
        service.run_pipeline(self.job.id, client=client)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoJob.Status.FAILED)
        self.assertIn("bad key", self.job.error)
        self.assertEqual(len(client.create_calls), 0)


class RequestVideoIdempotencyTests(TestCase):
    def setUp(self):
        self.page = make_blog_page()

    def test_second_request_returns_same_job_without_restarting(self):
        with mock.patch.object(service, "_start_pipeline") as start:
            first = service.request_video(self.page)
            second = service.request_video(self.page)

        self.assertEqual(first.id, second.id)
        self.assertEqual(VideoJob.objects.filter(page=self.page).count(), 1)
        # The pipeline is only started for the newly created job.
        self.assertEqual(start.call_count, 1)

    def test_new_request_allowed_after_failure(self):
        VideoJob.objects.create(
            page=self.page, status=VideoJob.Status.FAILED, error="boom"
        )
        with mock.patch.object(service, "_start_pipeline"):
            job = service.request_video(self.page)
        self.assertEqual(job.status, VideoJob.Status.PENDING)
        self.assertEqual(VideoJob.objects.filter(page=self.page).count(), 2)


class GetJobAndRefreshTests(TestCase):
    def setUp(self):
        self.page = make_blog_page()

    def test_get_job_rejects_unknown_id(self):
        with self.assertRaises(service.VideoJobNotFound):
            service.get_job(self.page, "00000000-0000-0000-0000-000000000000")

    def test_get_job_rejects_malformed_id(self):
        with self.assertRaises(service.VideoJobNotFound):
            service.get_job(self.page, "not-a-uuid")

    def test_refresh_download_url_resigns_when_ready(self):
        job = VideoJob.objects.create(
            page=self.page,
            status=VideoJob.Status.READY,
            project_id="proj_123",
            export_id="exp_123",
            download_url="https://old.example/expired.mp4",
        )
        client = FakeVideoGenClient(download_url="https://fresh.example/new.mp4")
        service.refresh_download_url(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.download_url, "https://fresh.example/new.mp4")

    def test_refresh_is_noop_for_non_ready_job(self):
        job = VideoJob.objects.create(page=self.page, status=VideoJob.Status.PROCESSING)
        client = FakeVideoGenClient()
        service.refresh_download_url(job, client=client)
        self.assertEqual(client.export_polls, 0)
