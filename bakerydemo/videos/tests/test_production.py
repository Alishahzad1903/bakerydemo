import tempfile
from unittest import mock

from django.test import TestCase, override_settings

from bakerydemo.videos import production
from bakerydemo.videos.exceptions import VideoGenProviderError
from bakerydemo.videos.models import ArticleVideo, VideoStatus

from ._helpers import make_blog_article


class FakeClient:
    """Stand-in for VideoGenClient that never touches the network."""

    def __init__(self, fail_at=None):
        self.fail_at = fail_at

    def _maybe_fail(self, step):
        if self.fail_at == step:
            raise VideoGenProviderError("provider exploded", status=500)

    def start_script_to_video(self, script):
        self._maybe_fail("start")
        return {"workflow_run_id": "vg_work_1", "project_id": "vg_proj_1"}

    def wait_for_workflow(self, workflow_run_id, on_progress=None):
        if on_progress:
            on_progress(100.0)
        self._maybe_fail("workflow")
        return {"status": "succeeded"}

    def start_export(self, project_id):
        self._maybe_fail("export")
        return "vg_expo_1"

    def wait_for_export(self, project_id, export_id):
        self._maybe_fail("wait_export")
        return {"status": "succeeded", "download_url": "https://x/y.mp4"}

    def download_export(self, export, output_path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x00\x00\x00\x20ftypisomFAKEMP4")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ProduceVideoTests(TestCase):
    def setUp(self):
        self.article = make_blog_article()
        self.av = ArticleVideo.objects.create(
            page=self.article, script="Title\n\nBody.", status=VideoStatus.PENDING
        )

    def test_successful_production_stores_ready_video(self):
        with mock.patch.object(
            production, "build_client_from_settings", return_value=FakeClient()
        ):
            production.produce_video(self.av.pk)

        self.av.refresh_from_db()
        self.assertEqual(self.av.status, VideoStatus.READY)
        self.assertEqual(self.av.progress_percentage, 100)
        self.assertTrue(self.av.video_file)
        self.assertEqual(self.av.error, "")
        self.assertEqual(self.av.workflow_run_id, "vg_work_1")
        self.assertEqual(self.av.project_id, "vg_proj_1")
        self.assertEqual(self.av.export_id, "vg_expo_1")
        self.assertTrue(self.av.is_ready)

    def test_provider_failure_marks_job_failed_with_message(self):
        with mock.patch.object(
            production,
            "build_client_from_settings",
            return_value=FakeClient(fail_at="start"),
        ):
            production.produce_video(self.av.pk)

        self.av.refresh_from_db()
        self.assertEqual(self.av.status, VideoStatus.FAILED)
        self.assertIn("provider exploded", self.av.error)
        self.assertFalse(self.av.video_file)
