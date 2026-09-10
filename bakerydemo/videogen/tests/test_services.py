from unittest import mock

from django.test import TestCase

from bakerydemo.videogen import services
from bakerydemo.videogen.exceptions import VideoGenServerError
from bakerydemo.videogen.models import ArticleVideo

from .utils import create_blog_article


class FakeClient:
    """Scripted VideoGenClient stand-in for driving the pipeline."""

    def __init__(self, *, run_states, export_states):
        self._run_states = list(run_states)
        self._export_states = list(export_states)
        self.created = 0
        self.exported = 0

    def create_script_to_video(self, **kwargs):
        self.created += 1
        self.script = kwargs.get("script")
        return {"workflowRunId": "wr_1", "projectId": "p_1"}

    def get_workflow_run(self, run_id):
        return self._run_states.pop(0)

    def export_project(self, project_id, **kwargs):
        self.exported += 1
        return {"exportId": "ex_1"}

    def get_project_export(self, project_id, export_id):
        return self._export_states.pop(0)


class ProducePipelineTests(TestCase):
    def setUp(self):
        self.article = create_blog_article()
        self.video = ArticleVideo.objects.create(
            page=self.article, script="Tracking Wild Yeast. Yeasts grow as single cells."
        )
        # No real sleeping between polls.
        patcher = mock.patch.object(services, "POLL_INTERVAL_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_with(self, client):
        with mock.patch(
            "bakerydemo.videogen.services.VideoGenClient.from_settings",
            return_value=client,
        ):
            services._produce(self.video.id)
        self.video.refresh_from_db()

    def test_happy_path_reaches_ready_with_one_video_and_one_export(self):
        client = FakeClient(
            run_states=[
                {"status": "running", "progressPercentage": 40},
                {"status": "succeeded", "projectId": "p_1", "progressPercentage": 100},
            ],
            export_states=[
                {"status": "running", "progressPercentage": 20},
                {
                    "status": "succeeded",
                    "progressPercentage": 100,
                    "downloadUrl": "https://dl.videogen/clip.mp4",
                    "downloadUrlExpiresAt": 4102444800,
                },
            ],
        )
        self._run_with(client)

        self.assertEqual(self.video.status, ArticleVideo.Status.READY)
        self.assertEqual(self.video.progress_percentage, 100)
        self.assertEqual(self.video.download_url, "https://dl.videogen/clip.mp4")
        self.assertIsNotNone(self.video.download_url_expires_at)
        self.assertEqual(self.video.error, "")
        # Exactly one billed production + one export.
        self.assertEqual(client.created, 1)
        self.assertEqual(client.exported, 1)

    def test_generation_failure_is_recorded_and_no_export_started(self):
        client = FakeClient(
            run_states=[{"status": "failed", "error": {"message": "bad script"}}],
            export_states=[],
        )
        self._run_with(client)

        self.assertEqual(self.video.status, ArticleVideo.Status.FAILED)
        self.assertEqual(self.video.error, "bad script")
        self.assertEqual(client.exported, 0)

    def test_export_failure_is_recorded(self):
        client = FakeClient(
            run_states=[{"status": "succeeded", "projectId": "p_1"}],
            export_states=[{"status": "failed", "error": "render crashed"}],
        )
        self._run_with(client)

        self.assertEqual(self.video.status, ArticleVideo.Status.FAILED)
        self.assertEqual(self.video.error, "render crashed")


class WorkerErrorHandlingTests(TestCase):
    def setUp(self):
        self.article = create_blog_article()
        self.video = ArticleVideo.objects.create(page=self.article, script="x")

    def test_worker_marks_failed_on_provider_error(self):
        boom = mock.Mock(side_effect=VideoGenServerError("provider down", status=500))
        with mock.patch(
            "bakerydemo.videogen.services.VideoGenClient.from_settings"
        ) as from_settings:
            from_settings.return_value.create_script_to_video = boom
            services._worker(self.video.id)

        self.video.refresh_from_db()
        self.assertEqual(self.video.status, ArticleVideo.Status.FAILED)
        self.assertIn("provider down", self.video.error)


class FreshDownloadUrlTests(TestCase):
    def setUp(self):
        self.article = create_blog_article()
        self.video = ArticleVideo.objects.create(
            page=self.article,
            script="x",
            status=ArticleVideo.Status.READY,
            project_id="p_1",
            export_id="ex_1",
            download_url="https://stale",
        )

    def test_resigns_and_caches_url(self):
        client = mock.Mock()
        client.get_project_export.return_value = {
            "downloadUrl": "https://fresh/clip.mp4",
            "downloadUrlExpiresAt": 4102444800,
        }
        with mock.patch(
            "bakerydemo.videogen.services.VideoGenClient.from_settings",
            return_value=client,
        ):
            url = services.get_fresh_download_url(self.video)
        self.assertEqual(url, "https://fresh/clip.mp4")
        self.video.refresh_from_db()
        self.assertEqual(self.video.download_url, "https://fresh/clip.mp4")
