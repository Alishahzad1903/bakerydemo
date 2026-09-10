from django.test import TestCase, override_settings
from wagtail.models import Page

from bakerydemo.videos import producer
from bakerydemo.videos.models import ArticleVideo
from bakerydemo.videos.service import JobSnapshot, StartResult


class FakeService:
    """A stand-in for VideoGenService that returns scripted snapshots."""

    def __init__(self, *, workflow_snaps, export_snaps, export_id="vg_expo_x"):
        self._workflow_snaps = list(workflow_snaps)
        self._export_snaps = list(export_snaps)
        self._start = StartResult("vg_work_x", "vg_proj_x")
        self._export_id = export_id
        self.calls: list[str] = []

    def start_script_to_video(self, script):
        self.calls.append("start")
        return self._start

    def get_workflow_run(self, workflow_run_id):
        self.calls.append("get_workflow")
        return self._pop(self._workflow_snaps)

    def export_project(self, project_id):
        self.calls.append("export")
        return self._export_id

    def get_project_export(self, project_id, export_id):
        self.calls.append("get_export")
        return self._pop(self._export_snaps)

    @staticmethod
    def _pop(snaps):
        # Keep returning the final (terminal) snapshot once exhausted.
        return snaps.pop(0) if len(snaps) > 1 else snaps[0]


@override_settings(VIDEOGEN_POLL_INTERVAL_SECONDS=0, VIDEOGEN_JOB_TIMEOUT_SECONDS=5)
class ProduceTests(TestCase):
    def setUp(self):
        self.page = Page.get_first_root_node()
        self.job = ArticleVideo.objects.create(page=self.page, script="Title. First sentence.")

    def _run(self, service):
        producer.produce(self.job, service=service)
        self.job.refresh_from_db()

    def test_full_flow_marks_ready(self):
        service = FakeService(
            workflow_snaps=[
                JobSnapshot(status="running", progress_percentage=40.0),
                JobSnapshot(status="succeeded", progress_percentage=100.0),
            ],
            export_snaps=[
                JobSnapshot(status="running", progress_percentage=50.0),
                JobSnapshot(
                    status="succeeded", progress_percentage=100.0, download_url="https://cdn/v.mp4"
                ),
            ],
        )
        self._run(service)

        self.assertEqual(self.job.status, ArticleVideo.Status.READY)
        self.assertEqual(self.job.progress_percentage, 100.0)
        self.assertEqual(self.job.download_url, "https://cdn/v.mp4")
        self.assertEqual(self.job.workflow_run_id, "vg_work_x")
        self.assertEqual(self.job.project_id, "vg_proj_x")
        self.assertEqual(self.job.export_id, "vg_expo_x")
        self.assertEqual(self.job.error, "")
        # Exactly one build + one export were requested.
        self.assertEqual(service.calls.count("start"), 1)
        self.assertEqual(service.calls.count("export"), 1)

    def test_workflow_failure_marks_failed_and_never_exports(self):
        service = FakeService(
            workflow_snaps=[
                JobSnapshot(status="failed", progress_percentage=0.0, error_message="bad script"),
            ],
            export_snaps=[JobSnapshot(status="succeeded", progress_percentage=100.0)],
        )
        self._run(service)

        self.assertEqual(self.job.status, ArticleVideo.Status.FAILED)
        self.assertIn("bad script", self.job.error)
        self.assertNotIn("export", service.calls)

    def test_export_failure_marks_failed(self):
        service = FakeService(
            workflow_snaps=[JobSnapshot(status="succeeded", progress_percentage=100.0)],
            export_snaps=[
                JobSnapshot(status="failed", progress_percentage=0.0, error_message="render error"),
            ],
        )
        self._run(service)

        self.assertEqual(self.job.status, ArticleVideo.Status.FAILED)
        self.assertIn("render error", self.job.error)

    def test_export_succeeded_without_url_marks_failed(self):
        service = FakeService(
            workflow_snaps=[JobSnapshot(status="succeeded", progress_percentage=100.0)],
            export_snaps=[JobSnapshot(status="succeeded", progress_percentage=100.0, download_url=None)],
        )
        self._run(service)

        self.assertEqual(self.job.status, ArticleVideo.Status.FAILED)
        self.assertIn("no download URL", self.job.error)

    def test_progress_advances_before_ready(self):
        service = FakeService(
            workflow_snaps=[
                JobSnapshot(status="running", progress_percentage=20.0),
                JobSnapshot(status="succeeded", progress_percentage=100.0),
            ],
            export_snaps=[
                JobSnapshot(
                    status="succeeded", progress_percentage=100.0, download_url="https://cdn/v.mp4"
                )
            ],
        )
        producer.produce(self.job, service=service)
        self.job.refresh_from_db()
        self.assertEqual(self.job.progress_percentage, 100.0)
