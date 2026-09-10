"""Tests for the article-to-video orchestration and cost-shape guarantees."""

from __future__ import annotations

import shutil
import tempfile

from django.test import TestCase, override_settings

from bakerydemo.videos import services
from bakerydemo.videos.constants import VideoJobStatus
from bakerydemo.videos.models import VideoJob
from bakerydemo.videos.videogen.client import StartedExport, StartedRun
from bakerydemo.videos.videogen.exceptions import (
    VideoGenAPIError,
    VideoGenProductionError,
)

from .factories import build_article


class MediaIsolatedTestCase(TestCase):
    """Base case that isolates ``MEDIA_ROOT`` so stored MP4s never touch the
    project's real media directory."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="videos-svc-test-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()


class FakeVideoGenClient:
    """Records the exact calls made, and returns canned successful data."""

    def __init__(self, *, file_bytes: bytes = b"MP4DATA", fail=None):
        self.started_scripts: list[str] = []
        self.export_calls: list[str] = []
        self.downloaded: list[str] = []
        self.closed = False
        self._file_bytes = file_bytes
        self._fail = fail  # optional exception to raise from start_script_to_video

    def start_script_to_video(self, script):
        self.started_scripts.append(script)
        if self._fail is not None:
            raise self._fail
        return StartedRun(workflow_run_id="vg_work_1", project_id="vg_proj_1")

    def wait_for_run(self, run_id, *, on_progress=None):
        if on_progress:
            on_progress(50.0)
            on_progress(100.0)
        return {
            "status": "succeeded",
            "project_id": "vg_proj_1",
            "progress_percentage": 100,
        }

    def start_export(self, project_id):
        self.export_calls.append(project_id)
        return StartedExport(export_id="vg_exp_1")

    def wait_for_export(self, project_id, export_id, *, on_progress=None):
        if on_progress:
            on_progress(100.0)
        return {
            "status": "succeeded",
            "export_file_id": "vg_file_1",
            "progress_percentage": 100,
        }

    def download_file(self, file_id):
        self.downloaded.append(file_id)
        return self._file_bytes

    def close(self):
        self.closed = True


class PipelineSuccessTests(MediaIsolatedTestCase):
    def setUp(self):
        self.article = build_article()
        self.job = VideoJob.objects.create(
            page=self.article,
            script=services.script_for_article(self.article),
            status=VideoJobStatus.PENDING,
        )

    def test_pipeline_produces_and_stores_video(self):
        fake = FakeVideoGenClient(file_bytes=b"REALMP4BYTES")
        services.run_pipeline(str(self.job.pk), client=fake)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoJobStatus.SUCCEEDED)
        self.assertEqual(self.job.progress_percentage, 100)
        self.assertTrue(self.job.video_file)
        self.assertEqual(self.job.video_file.read(), b"REALMP4BYTES")
        self.assertEqual(self.job.videogen_workflow_run_id, "vg_work_1")
        self.assertEqual(self.job.videogen_project_id, "vg_proj_1")
        self.assertEqual(self.job.videogen_export_id, "vg_exp_1")
        self.assertEqual(self.job.videogen_file_id, "vg_file_1")
        self.assertTrue(fake.closed is False)  # client is caller-owned here

    def test_narration_is_title_plus_first_sentence(self):
        fake = FakeVideoGenClient()
        services.run_pipeline(str(self.job.pk), client=fake)
        self.assertEqual(
            fake.started_scripts,
            [
                "Tracking Wild Yeast. Yeasts, with their single-celled growth "
                "habit, can be contrasted with molds, which grow hyphae."
            ],
        )

    def test_exactly_one_run_and_one_export(self):
        fake = FakeVideoGenClient()
        services.run_pipeline(str(self.job.pk), client=fake)
        self.assertEqual(len(fake.started_scripts), 1)
        self.assertEqual(len(fake.export_calls), 1)
        self.assertEqual(len(fake.downloaded), 1)


class PipelineFailureTests(MediaIsolatedTestCase):
    def setUp(self):
        self.article = build_article()
        self.job = VideoJob.objects.create(
            page=self.article,
            script="x",
            status=VideoJobStatus.PENDING,
        )

    def test_provider_failure_is_recorded_not_raised(self):
        fake = FakeVideoGenClient(
            fail=VideoGenProductionError(
                "VideoGen workflow run failed: bad script", provider_status="failed"
            )
        )
        # run_pipeline records the failure instead of raising.
        services.run_pipeline(str(self.job.pk), client=fake)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoJobStatus.FAILED)
        self.assertIn("failed", self.job.error_message.lower())
        self.assertFalse(self.job.video_file)

    def test_api_error_is_recorded(self):
        fake = FakeVideoGenClient(
            fail=VideoGenAPIError("boom", status=500, request_id="req_1")
        )
        services.run_pipeline(str(self.job.pk), client=fake)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, VideoJobStatus.FAILED)
        self.assertIn("boom", self.job.error_message)


class IdempotencyTests(MediaIsolatedTestCase):
    def test_second_request_returns_same_job(self):
        article = build_article()
        calls = []
        job1, created1 = services.request_video(
            article, user=None, runner=lambda jid: calls.append(jid)
        )
        job2, created2 = services.request_video(
            article, user=None, runner=lambda jid: calls.append(jid)
        )
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(job1.pk, job2.pk)
        self.assertEqual(VideoJob.objects.filter(page=article).count(), 1)

    def test_failed_job_allows_a_new_attempt(self):
        article = build_article()
        job1, _ = services.request_video(article, user=None, runner=lambda jid: None)
        job1.status = VideoJobStatus.FAILED
        job1.save(update_fields=["status"])

        job2, created2 = services.request_video(
            article, user=None, runner=lambda jid: None
        )
        self.assertTrue(created2)
        self.assertNotEqual(job1.pk, job2.pk)

    def test_client_request_shape_is_cheap(self):
        """The build payload contains only the cheap-shape fields."""
        from bakerydemo.videos.videogen.client import VideoGenClient, VideoGenConfig

        # Defaults mirror settings: stock footage, no workflow-quality override.
        config = VideoGenConfig(api_key="test-key")
        client = VideoGenClient(config=config)

        captured = {}

        class _SDK:
            class workflows:
                @staticmethod
                def script_to_video(**kwargs):
                    captured.update(kwargs)
                    return {"workflow_run_id": "vg_work_x", "project_id": "vg_proj_x"}

        client._sdk = _SDK()  # inject a stand-in SDK
        client.start_script_to_video("Title. Sentence.")

        self.assertEqual(captured.get("script"), "Title. Sentence.")
        # Stock footage only — never AI-generated imagery.
        self.assertEqual(captured.get("visual_style"), {"type": "STOCK"})
        # No remix actions (captions, image-to-video, transitions), no
        # avatar/presenter, no workflow-quality override.
        self.assertNotIn("remix_actions", captured)
        self.assertNotIn("actor_entity_id", captured)
        self.assertNotIn("quality", captured)

    def test_export_uses_configured_quality(self):
        """The export request pins the 720p-class 'STANDARD' tier (never 4K)."""
        from bakerydemo.videos.videogen.client import VideoGenClient, VideoGenConfig

        config = VideoGenConfig(api_key="test-key")
        client = VideoGenClient(config=config)

        captured = {}

        class _SDK:
            class projects:
                @staticmethod
                def export_project(**kwargs):
                    captured.update(kwargs)
                    return {"export_id": "vg_exp_x"}

        client._sdk = _SDK()
        client.start_export("vg_proj_x")
        self.assertEqual(captured.get("project_id"), "vg_proj_x")
        self.assertEqual(captured.get("quality"), "STANDARD")
