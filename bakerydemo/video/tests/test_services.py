from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from bakerydemo.video import services
from bakerydemo.video.client import VideoGenClient
from bakerydemo.video.exceptions import VideoGenBadRequestError
from bakerydemo.video.models import ArticleVideo, VideoStatus

from .utils import create_blog_article


def _mock_client():
    return mock.Mock(spec=VideoGenClient)


# Make polling instant in tests.
@override_settings(VIDEOGEN_POLL_INTERVAL=0, VIDEOGEN_POLL_TIMEOUT=5)
class ProductionPipelineTests(TestCase):
    def setUp(self):
        self.article = create_blog_article()
        self.job = ArticleVideo.objects.create(
            page=self.article, script="Tracking Wild Yeast. Yeasts are great."
        )

    def test_happy_path_produces_ready_video(self):
        client = _mock_client()
        client.create_script_to_video.return_value = {
            "workflowRunId": "wr_1",
            "projectId": "pr_1",
        }
        client.get_workflow_run.return_value = {
            "status": "succeeded",
            "progressPercentage": 100,
            "projectId": "pr_1",
        }
        client.export_project.return_value = {"exportId": "ex_1"}
        client.get_export.return_value = {
            "status": "succeeded",
            "progressPercentage": 100,
            "downloadUrl": "https://signed.example/video.mp4",
        }

        services.run_production_pipeline(self.job, client)
        self.job.refresh_from_db()

        self.assertEqual(self.job.status, VideoStatus.READY)
        self.assertEqual(self.job.progress_percentage, 100)
        self.assertEqual(self.job.download_url, "https://signed.example/video.mp4")
        self.assertEqual(self.job.error, "")

    def test_only_one_workflow_and_one_export(self):
        client = _mock_client()
        client.create_script_to_video.return_value = {"workflowRunId": "wr", "projectId": "pr"}
        client.get_workflow_run.return_value = {"status": "succeeded", "projectId": "pr"}
        client.export_project.return_value = {"exportId": "ex"}
        client.get_export.return_value = {
            "status": "succeeded",
            "downloadUrl": "https://signed.example/v.mp4",
        }

        services.run_production_pipeline(self.job, client)

        self.assertEqual(client.create_script_to_video.call_count, 1)
        self.assertEqual(client.export_project.call_count, 1)
        # Exactly one export of the project; the 720p (HIGH) tier is the
        # client's default and is asserted in the client tests.
        client.export_project.assert_called_once_with("pr")

    def test_script_to_video_called_with_the_jobs_script(self):
        client = _mock_client()
        client.create_script_to_video.return_value = {"workflowRunId": "wr", "projectId": "pr"}
        client.get_workflow_run.return_value = {"status": "succeeded", "projectId": "pr"}
        client.export_project.return_value = {"exportId": "ex"}
        client.get_export.return_value = {"status": "succeeded", "downloadUrl": "u"}

        services.run_production_pipeline(self.job, client)
        client.create_script_to_video.assert_called_once_with(
            script="Tracking Wild Yeast. Yeasts are great."
        )

    def test_workflow_failure_is_recorded_and_export_not_attempted(self):
        client = _mock_client()
        client.create_script_to_video.return_value = {"workflowRunId": "wr", "projectId": "pr"}
        client.get_workflow_run.return_value = {
            "status": "failed",
            "error": {"message": "script rejected", "code": "invalid_parameters"},
        }

        services.run_production_pipeline(self.job, client)
        self.job.refresh_from_db()

        self.assertEqual(self.job.status, VideoStatus.FAILED)
        self.assertIn("script rejected", self.job.error)
        client.export_project.assert_not_called()

    def test_export_failure_is_recorded(self):
        client = _mock_client()
        client.create_script_to_video.return_value = {"workflowRunId": "wr", "projectId": "pr"}
        client.get_workflow_run.return_value = {"status": "succeeded", "projectId": "pr"}
        client.export_project.return_value = {"exportId": "ex"}
        client.get_export.return_value = {"status": "failed", "error": "render crashed"}

        services.run_production_pipeline(self.job, client)
        self.job.refresh_from_db()

        self.assertEqual(self.job.status, VideoStatus.FAILED)
        self.assertIn("render crashed", self.job.error)

    def test_provider_http_error_is_recorded_as_failure(self):
        client = _mock_client()
        client.create_script_to_video.side_effect = VideoGenBadRequestError(
            "bad script", status=400, code="invalid_parameters"
        )

        services.run_production_pipeline(self.job, client)
        self.job.refresh_from_db()

        self.assertEqual(self.job.status, VideoStatus.FAILED)
        self.assertIn("bad script", self.job.error)


class StartVideoIdempotencyTests(TestCase):
    def setUp(self):
        self.article = create_blog_article()
        patcher = mock.patch.object(services, "spawn_worker")
        self.spawn = patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_call_creates_and_spawns(self):
        job, created = services.start_video_for_page(self.article)
        self.assertTrue(created)
        self.assertEqual(ArticleVideo.objects.count(), 1)
        self.spawn.assert_called_once_with(job.id)
        self.assertTrue(job.script.startswith("Tracking Wild Yeast."))

    def test_second_call_reuses_and_does_not_spawn_again(self):
        job1, _ = services.start_video_for_page(self.article)
        self.spawn.reset_mock()
        job2, created = services.start_video_for_page(self.article)
        self.assertFalse(created)
        self.assertEqual(job1.id, job2.id)
        self.assertEqual(ArticleVideo.objects.count(), 1)
        self.spawn.assert_not_called()


class RefreshDownloadUrlTests(TestCase):
    def setUp(self):
        self.article = create_blog_article()

    def test_stale_url_is_refreshed(self):
        job = ArticleVideo.objects.create(
            page=self.article,
            status=VideoStatus.READY,
            project_id="pr",
            export_id="ex",
            download_url="https://old.example/v.mp4",
            download_url_signed_at=timezone.now() - timezone.timedelta(days=30),
        )
        client = _mock_client()
        client.get_export.return_value = {
            "status": "succeeded",
            "downloadUrl": "https://fresh.example/v.mp4",
        }
        services.refresh_download_url_if_stale(job, client)
        job.refresh_from_db()
        self.assertEqual(job.download_url, "https://fresh.example/v.mp4")
        client.get_export.assert_called_once_with("pr", "ex")

    def test_fresh_url_is_not_refreshed(self):
        job = ArticleVideo.objects.create(
            page=self.article,
            status=VideoStatus.READY,
            project_id="pr",
            export_id="ex",
            download_url="https://current.example/v.mp4",
            download_url_signed_at=timezone.now(),
        )
        client = _mock_client()
        services.refresh_download_url_if_stale(job, client)
        client.get_export.assert_not_called()

    def test_processing_job_never_calls_provider(self):
        job = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.PROCESSING
        )
        client = _mock_client()
        services.refresh_download_url_if_stale(job, client)
        client.get_export.assert_not_called()
