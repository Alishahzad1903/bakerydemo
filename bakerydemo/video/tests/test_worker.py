"""State-machine tests for the production pipeline, with a fake gateway.

The gateway is replaced by a scripted fake so no VideoGen calls happen. These
assert how the worker advances the job, how provider failures become a FAILED
job, and that the billable steps are started at most once.
"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from bakerydemo.video.models import ArticleVideo, VideoStatus
from bakerydemo.video.worker import produce

from .utils import create_blog_article


def running(pct=10.0):
    return SimpleNamespace(status="running", progress_percentage=pct, error=None)


def workflow_succeeded():
    return SimpleNamespace(status="succeeded", progress_percentage=100.0, error=None)


def failed(message="it went wrong"):
    return SimpleNamespace(
        status="failed",
        progress_percentage=50.0,
        error=SimpleNamespace(message=message),
        download_url=None,
    )


def export_succeeded(url="https://videogen.example/signed/video.mp4"):
    return SimpleNamespace(
        status="succeeded",
        progress_percentage=100.0,
        download_url=url,
        error=None,
    )


def export_no_url():
    return SimpleNamespace(
        status="succeeded", progress_percentage=100.0, download_url=None, error=None
    )


class FakeGateway:
    def __init__(self, workflow_states, export_states, video_bytes=b"FAKE-MP4-BYTES"):
        self._workflow_states = list(workflow_states)
        self._export_states = list(export_states)
        self._video_bytes = video_bytes
        self.started = 0
        self.exported = 0
        self.downloaded_urls: list[str] = []

    def start_script_to_video(self, script):
        self.started += 1
        return ("wf_generated", "proj_generated")

    def get_workflow_run(self, workflow_run_id):
        return self._workflow_states.pop(0)

    def export_project(self, project_id):
        self.exported += 1
        return "exp_generated"

    def get_project_export(self, project_id, export_id):
        return self._export_states.pop(0)

    def download_mp4(self, url):
        self.downloaded_urls.append(url)
        return self._video_bytes


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
@patch("bakerydemo.video.worker.time.sleep", lambda *a, **k: None)
class WorkerPipelineTests(TestCase):
    def setUp(self):
        self.page = create_blog_article()
        self.job = ArticleVideo.objects.create(
            page=self.page, script="Title. First sentence", status=VideoStatus.PENDING
        )

    def test_happy_path_reaches_ready_with_stored_file(self):
        gateway = FakeGateway(
            workflow_states=[running(), workflow_succeeded()],
            export_states=[running(80.0), export_succeeded()],
        )
        produce(self.job, gateway)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoStatus.READY)
        self.assertEqual(self.job.progress_percentage, 100)
        self.assertTrue(self.job.video_file)
        self.assertEqual(self.job.video_file.read(), b"FAKE-MP4-BYTES")
        self.assertEqual(self.job.error, "")
        # Exactly one workflow and one export — never double-billed.
        self.assertEqual(gateway.started, 1)
        self.assertEqual(gateway.exported, 1)
        self.assertEqual(len(gateway.downloaded_urls), 1)

    def test_workflow_failure_marks_failed_and_never_exports(self):
        gateway = FakeGateway(
            workflow_states=[failed("production blew up")],
            export_states=[],
        )
        produce(self.job, gateway)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoStatus.FAILED)
        self.assertIn("production blew up", self.job.error)
        self.assertEqual(gateway.exported, 0)

    def test_export_failure_marks_failed(self):
        gateway = FakeGateway(
            workflow_states=[workflow_succeeded()],
            export_states=[failed("export blew up")],
        )
        produce(self.job, gateway)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoStatus.FAILED)
        self.assertIn("export blew up", self.job.error)

    def test_succeeded_export_without_url_marks_failed(self):
        gateway = FakeGateway(
            workflow_states=[workflow_succeeded()],
            export_states=[export_no_url()],
        )
        produce(self.job, gateway)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoStatus.FAILED)
        self.assertIn("download URL", self.job.error)

    def test_does_not_restart_workflow_already_started(self):
        # Simulate a job whose workflow/export were already created.
        self.job.provider_workflow_run_id = "wf_preexisting"
        self.job.provider_project_id = "proj_preexisting"
        self.job.provider_export_id = "exp_preexisting"
        self.job.save()

        gateway = FakeGateway(
            workflow_states=[workflow_succeeded()],
            export_states=[export_succeeded()],
        )
        produce(self.job, gateway)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoStatus.READY)
        # Neither billable step was re-issued.
        self.assertEqual(gateway.started, 0)
        self.assertEqual(gateway.exported, 0)
