from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from wagtail.models import Page

from bakerydemo.videos import service
from bakerydemo.videos.models import JobStatus, VideoJob


def _stage(status, progress, error=None, download_url=None, expires=None):
    return SimpleNamespace(
        status=status,
        progress_percentage=progress,
        error=error,
        download_url=download_url,
        download_url_expires_at=expires,
    )


class FakeService:
    """Stand-in for VideoGenService driven by scripted provider states."""

    def __init__(self, workflow_states, export_states):
        self._workflow_states = list(workflow_states)
        self._export_states = list(export_states)
        self.script_calls = 0
        self.export_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def _next(self, states):
        return states.pop(0) if len(states) > 1 else states[0]

    def start_script_to_video(self, script):
        self.script_calls += 1
        self.script = script
        return SimpleNamespace(workflow_run_id="wr_1", project_id="pr_1")

    def get_workflow_run(self, workflow_run_id):
        return self._next(self._workflow_states)

    def export_project(self, project_id):
        self.export_calls += 1
        return SimpleNamespace(export_id="ex_1")

    def get_project_export(self, project_id, export_id):
        return self._next(self._export_states)


class PipelineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        cls.page = root.add_child(instance=Page(title="Article", slug="article-x"))

    def _job(self):
        return VideoJob.objects.create(
            page=self.page, status=JobStatus.PENDING, script="Title. Sentence."
        )

    def _run(self, fake):
        with mock.patch.object(service, "POLL_INTERVAL_SECONDS", 0):
            service.run_pipeline(self._job_id, service_factory=lambda: fake)

    def test_success_path_downloads_url_and_bills_once(self):
        job = self._job()
        self._job_id = job.id
        fake = FakeService(
            workflow_states=[_stage("running", 50), _stage("succeeded", 100)],
            export_states=[
                _stage(
                    "succeeded",
                    100,
                    download_url="https://cdn/v.mp4",
                    expires=9_999_999_999,
                )
            ],
        )
        self._run(fake)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.SUCCEEDED)
        self.assertEqual(job.progress_percentage, 100.0)
        self.assertEqual(job.download_url, "https://cdn/v.mp4")
        self.assertEqual(job.download_url_expires_at, 9_999_999_999)
        # Each billable call happened exactly once.
        self.assertEqual(fake.script_calls, 1)
        self.assertEqual(fake.export_calls, 1)
        self.assertTrue(fake.closed)

    def test_workflow_failure_marks_failed_without_exporting(self):
        job = self._job()
        self._job_id = job.id
        fake = FakeService(
            workflow_states=[
                _stage(
                    "failed",
                    20,
                    error=SimpleNamespace(message="bad script", code="invalid_request"),
                )
            ],
            export_states=[_stage("succeeded", 100)],
        )
        self._run(fake)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_message, "bad script")
        self.assertEqual(job.error_code, "invalid_request")
        self.assertEqual(fake.script_calls, 1)
        self.assertEqual(fake.export_calls, 0)  # never re-attempts / never exports

    def test_export_failure_marks_failed(self):
        job = self._job()
        self._job_id = job.id
        fake = FakeService(
            workflow_states=[_stage("succeeded", 100)],
            export_states=[
                _stage(
                    "failed",
                    40,
                    error=SimpleNamespace(message="render failed", code="x"),
                )
            ],
        )
        self._run(fake)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_message, "render failed")
        self.assertEqual(fake.export_calls, 1)


class IdempotencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        cls.page = root.add_child(instance=Page(title="Idem", slug="idem-x"))

    def test_second_request_returns_same_job(self):
        job1, created1 = service.start_or_get_job(self.page, "s", spawn=False)
        job2, created2 = service.start_or_get_job(self.page, "s", spawn=False)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(job1.id, job2.id)
        self.assertEqual(VideoJob.objects.filter(page=self.page).count(), 1)

    def test_failed_job_allows_a_new_one(self):
        job1, _ = service.start_or_get_job(self.page, "s", spawn=False)
        VideoJob.objects.filter(pk=job1.id).update(status=JobStatus.FAILED)
        job2, created2 = service.start_or_get_job(self.page, "s", spawn=False)
        self.assertTrue(created2)
        self.assertNotEqual(job1.id, job2.id)


class RefreshDownloadUrlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        cls.page = root.add_child(instance=Page(title="Refresh", slug="refresh-x"))

    def _succeeded_job(self, **kw):
        defaults = {
            "page": self.page,
            "status": JobStatus.SUCCEEDED,
            "project_id": "pr_1",
            "export_id": "ex_1",
            "progress_percentage": 100.0,
        }
        defaults.update(kw)
        return VideoJob.objects.create(**defaults)

    def test_fresh_url_is_reused_without_calling_provider(self):
        job = self._succeeded_job(
            download_url="https://cdn/fresh.mp4",
            download_url_expires_at=9_999_999_999,
        )

        def boom():
            raise AssertionError("provider must not be called for a fresh URL")

        url = service.refresh_download_url(job, service_factory=boom, now=1000.0)
        self.assertEqual(url, "https://cdn/fresh.mp4")

    def test_expiring_url_is_refreshed(self):
        job = self._succeeded_job(
            download_url="https://cdn/stale.mp4",
            download_url_expires_at=1000,  # already effectively expired
        )
        fake = FakeService(
            workflow_states=[_stage("succeeded", 100)],
            export_states=[
                _stage(
                    "succeeded",
                    100,
                    download_url="https://cdn/new.mp4",
                    expires=9_999_999_999,
                )
            ],
        )
        url = service.refresh_download_url(job, service_factory=lambda: fake, now=999.0)
        self.assertEqual(url, "https://cdn/new.mp4")
        job.refresh_from_db()
        self.assertEqual(job.download_url, "https://cdn/new.mp4")
