from unittest import mock

from django.test import TestCase, override_settings

from bakerydemo.videos import services
from bakerydemo.videos.exceptions import (
    VideoGenConfigurationError,
    VideoGenJobFailedError,
)
from bakerydemo.videos.models import ArticleVideo

from .factories import TempMediaRootMixin, create_blog_article


class FakeClient:
    """Records calls and returns canned VideoGen responses."""

    def __init__(self, *, run_status="succeeded", export_status="succeeded"):
        self.run_status = run_status
        self.export_status = export_status
        self.create_kwargs = None
        self.export_kwargs = None
        self.create_calls = 0
        self.export_calls = 0
        self.downloaded = []

    def create_script_to_video(self, **kwargs):
        self.create_calls += 1
        self.create_kwargs = kwargs
        return {"workflowRunId": "vg_work_1", "projectId": "vg_proj_1"}

    def get_workflow_run(self, run_id):
        state = {
            "status": self.run_status,
            "progressPercentage": 100,
            "projectId": "vg_proj_1",
            "error": None,
        }
        if self.run_status == "failed":
            state["error"] = {"message": "render exploded", "code": "render_error"}
        return state

    def export_project(self, project_id, *, quality):
        self.export_calls += 1
        self.export_kwargs = {"project_id": project_id, "quality": quality}
        return {"exportId": "vg_expo_1"}

    def get_project_export(self, project_id, export_id):
        state = {
            "status": self.export_status,
            "progressPercentage": 100,
            "downloadUrl": "https://videogen.example/signed/output.mp4",
            "exportFileId": "vg_file_1",
            "file": None,
            "error": None,
        }
        if self.export_status == "failed":
            state["error"] = {"message": "export exploded", "code": "export_error"}
        return state

    def hydrate_file(self, file_id):
        return {"downloadSource": {"url": "https://videogen.example/hydrated.mp4"}}

    def download_to(self, url, destination):
        data = b"FAKE-MP4-BYTES"
        destination.write(data)
        self.downloaded.append(url)
        return len(data)


@override_settings(VIDEOGEN_API_KEY="test-key", VIDEOGEN_POLL_INTERVAL_SECONDS=0)
class ProducePipelineTests(TempMediaRootMixin, TestCase):
    def _make_job(self):
        article = create_blog_article()
        return ArticleVideo.objects.create(
            page=article.page_ptr, script="Tracking Wild Yeast. Yeasts grow."
        )

    def test_happy_path_requests_only_the_cheap_shape(self):
        video = self._make_job()
        client = FakeClient()

        services._produce(client, video)

        video.refresh_from_db()
        self.assertEqual(video.status, ArticleVideo.Status.SUCCEEDED)
        self.assertEqual(video.progress_percentage, 100)
        self.assertTrue(video.video_file)
        self.assertEqual(video.video_bytes, len(b"FAKE-MP4-BYTES"))
        self.assertEqual(video.video_file.read(), b"FAKE-MP4-BYTES")

        # Cheap shape: stock visuals, 16:9, exactly one 720p export.
        self.assertEqual(client.create_kwargs["visual_style"], "STOCK")
        self.assertEqual(client.create_kwargs["aspect_width"], 16)
        self.assertEqual(client.create_kwargs["aspect_height"], 9)
        self.assertEqual(client.export_kwargs["quality"], "STANDARD")
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.export_calls, 1)

    def test_export_is_not_repeated_on_resume(self):
        video = self._make_job()
        client = FakeClient()
        services._produce(client, video)
        first_export_calls = client.export_calls

        # Running again (e.g. a resumed pipeline) must not export a second time.
        services._produce(client, video)
        self.assertEqual(client.export_calls, first_export_calls)
        self.assertEqual(client.create_calls, 1)

    def test_render_failure_raises_typed_error(self):
        video = self._make_job()
        client = FakeClient(run_status="failed")
        with self.assertRaises(VideoGenJobFailedError) as ctx:
            services._produce(client, video)
        self.assertEqual(ctx.exception.code, "render_error")
        self.assertEqual(client.export_calls, 0)  # never got to export

    def test_run_pipeline_records_failure_on_the_row(self):
        video = self._make_job()
        client = FakeClient(export_status="failed")
        with mock.patch.object(services, "get_videogen_client", return_value=client):
            services.run_pipeline(video.pk)
        video.refresh_from_db()
        self.assertEqual(video.status, ArticleVideo.Status.FAILED)
        self.assertIn("export exploded", video.error_message)
        self.assertEqual(video.error_code, "export_error")


@override_settings(VIDEOGEN_API_KEY="test-key")
class StartVideoForPageTests(TestCase):
    def test_is_idempotent_and_launches_once(self):
        article = create_blog_article()
        with mock.patch.object(services, "launch_pipeline") as launch:
            video1, created1 = services.start_video_for_page(article)
            video2, created2 = services.start_video_for_page(article)

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(video1.pk, video2.pk)
        self.assertEqual(ArticleVideo.objects.count(), 1)
        launch.assert_called_once_with(video1.pk)

    def test_missing_api_key_raises_configuration_error(self):
        article = create_blog_article()
        with override_settings(VIDEOGEN_API_KEY=""):
            with self.assertRaises(VideoGenConfigurationError):
                services.start_video_for_page(article)
        self.assertEqual(ArticleVideo.objects.count(), 0)
