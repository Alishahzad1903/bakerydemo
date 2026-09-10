from unittest import mock

from django.test import TestCase

from bakerydemo.videogen import services
from bakerydemo.videogen.exceptions import VideoGenWorkflowError
from bakerydemo.videogen.models import VideoJob, VideoJobStatus

from .factories import build_blog_tree


class FakeClient:
    """A stand-in VideoGenClient that returns canned data without any network."""

    def __init__(self, *, fail_workflow=False, download=b"FAKE_MP4"):
        self.fail_workflow = fail_workflow
        self.download_bytes = download
        self.calls = []

    def create_script_to_video(self, *, script, aspect_ratio=(16, 9)):
        self.calls.append(("create", script))
        return {"workflowRunId": "vg_work_1", "projectId": "vg_proj_1"}

    def poll_workflow_run(self, run_id, *, on_progress=None):
        self.calls.append(("poll_run", run_id))
        if on_progress:
            on_progress({"status": "running", "progressPercentage": 40})
        if self.fail_workflow:
            raise VideoGenWorkflowError("workflow failed", code="boom")
        if on_progress:
            on_progress({"status": "succeeded", "progressPercentage": 100})
        return {"status": "succeeded"}

    def export_project(self, project_id, *, quality="STANDARD"):
        self.calls.append(("export", project_id, quality))
        return {"exportId": "vg_expo_1"}

    def poll_project_export(self, project_id, export_id, *, on_progress=None):
        self.calls.append(("poll_export", export_id))
        if on_progress:
            on_progress({"status": "succeeded", "progressPercentage": 100})
        return {
            "status": "succeeded",
            "downloadUrl": "https://signed.example/video.mp4",
            "exportFileId": "vg_file_1",
        }

    def download(self, url):
        self.calls.append(("download", url))
        return self.download_bytes


class GetOrCreateVideoJobTests(TestCase):
    def setUp(self):
        self.tree = build_blog_tree()
        self.article = self.tree["article"]

    def test_creates_one_job_and_enqueues_once(self):
        with mock.patch.object(services, "enqueue") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                job, created = services.get_or_create_video_job(self.article)
        self.assertTrue(created)
        self.assertEqual(VideoJob.objects.count(), 1)
        enqueue.assert_called_once_with(job.pk)
        # Narration is title + first intro sentence, the article's own words.
        self.assertIn("Tracking Wild Yeast", job.narration_script)
        self.assertIn("single-celled", job.narration_script)
        self.assertNotIn("dimorphic", job.narration_script)  # later sentence excluded

    def test_second_request_returns_same_job_without_enqueue(self):
        with mock.patch.object(services, "enqueue") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                job1, created1 = services.get_or_create_video_job(self.article)
            with self.captureOnCommitCallbacks(execute=True):
                job2, created2 = services.get_or_create_video_job(self.article)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(job1.pk, job2.pk)
        self.assertEqual(VideoJob.objects.count(), 1)
        enqueue.assert_called_once()  # not billed / produced twice


class RunVideoJobTests(TestCase):
    def setUp(self):
        self.tree = build_blog_tree()
        self.job = VideoJob.objects.create(
            page=self.tree["article"],
            narration_script="Tracking Wild Yeast. Yeasts can be contrasted with molds.",
        )

    def test_success_stores_mp4_and_marks_ready(self):
        client = FakeClient()
        job = services.run_video_job(self.job.pk, client=client)
        self.assertEqual(job.status, VideoJobStatus.READY)
        self.assertEqual(job.progress_percentage, 100)
        self.assertTrue(job.video_file)
        self.assertEqual(job.video_file.read(), b"FAKE_MP4")
        self.assertEqual(job.export_file_id, "vg_file_1")
        self.assertEqual(job.download_url(), job.video_file.url)
        # Exactly one workflow and one export.
        self.assertEqual(sum(1 for c in client.calls if c[0] == "create"), 1)
        self.assertEqual(sum(1 for c in client.calls if c[0] == "export"), 1)
        # Export requested at STANDARD (720p, never 4K).
        export_call = next(c for c in client.calls if c[0] == "export")
        self.assertEqual(export_call[2], "STANDARD")
        job.video_file.delete(save=False)

    def test_failure_records_typed_error(self):
        client = FakeClient(fail_workflow=True)
        job = services.run_video_job(self.job.pk, client=client)
        self.assertEqual(job.status, VideoJobStatus.FAILED)
        self.assertIn("workflow failed", job.error)
        self.assertEqual(job.error_code, "boom")
        self.assertFalse(job.video_file)
        # No export attempted after the workflow failed.
        self.assertFalse(any(c[0] == "export" for c in client.calls))

    def test_resume_does_not_recreate_workflow_or_export(self):
        # Simulate a job that already got as far as an export id.
        self.job.workflow_run_id = "vg_work_1"
        self.job.project_id = "vg_proj_1"
        self.job.export_id = "vg_expo_1"
        self.job.save()
        client = FakeClient()
        job = services.run_video_job(self.job.pk, client=client)
        self.assertEqual(job.status, VideoJobStatus.READY)
        self.assertFalse(any(c[0] == "create" for c in client.calls))
        self.assertFalse(any(c[0] == "export" for c in client.calls))
        job.video_file.delete(save=False)
