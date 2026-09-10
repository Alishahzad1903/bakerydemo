from bakerydemo.videogen.models import JobStatus, VideoJob
from bakerydemo.videogen.service import advance_job, start_job

from .base import BakeryVideoTestCase
from .fakes import BoomError, FakeVideoGenClient, failed, running, succeeded


class StartJobTests(BakeryVideoTestCase):
    def test_start_creates_job_and_starts_one_run(self):
        client = FakeVideoGenClient()
        job = start_job(self.article, client=client)

        self.assertEqual(job.status, JobStatus.GENERATING)
        self.assertEqual(job.workflow_run_id, "wr_test")
        self.assertEqual(job.project_id, "pr_test")
        self.assertEqual(client.create_calls, [job.script])
        # Narration is built from the article's own title + first intro sentence.
        self.assertEqual(job.script, "Tracking Wild Yeast. Wild yeast is everywhere.")

    def test_start_is_idempotent_no_second_video(self):
        client1 = FakeVideoGenClient()
        job1 = start_job(self.article, client=client1)

        client2 = FakeVideoGenClient()
        job2 = start_job(self.article, client=client2)

        self.assertEqual(job1.pk, job2.pk)
        # The second call must not start another video.
        self.assertEqual(client2.create_calls, [])
        self.assertEqual(VideoJob.objects.count(), 1)

    def test_start_failure_marks_failed_and_is_not_retried(self):
        client = FakeVideoGenClient(create_error=BoomError("nope"))
        job = start_job(self.article, client=client)

        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertIn("nope", job.error)

        # A subsequent request returns the same failed job, never a new video.
        client2 = FakeVideoGenClient()
        job2 = start_job(self.article, client=client2)
        self.assertEqual(job2.pk, job.pk)
        self.assertEqual(client2.create_calls, [])


class AdvanceJobTests(BakeryVideoTestCase):
    def _start(self, **kwargs):
        return start_job(self.article, client=FakeVideoGenClient())

    def test_full_happy_path_to_ready(self):
        job = self._start()

        # 1) generation still running
        client = FakeVideoGenClient(
            run_states=[running(50)],
        )
        advance_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.GENERATING)
        self.assertGreater(job.progress_percentage, 0)

        # 2) generation succeeds -> exactly one export started
        client = FakeVideoGenClient(run_states=[succeeded(projectId="pr_test")])
        advance_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.EXPORTING)
        self.assertEqual(job.export_id, "ex_test")
        self.assertEqual(client.export_calls, [("pr_test", "STANDARD")])

        # 3) export still running
        client = FakeVideoGenClient(export_states=[running(40)])
        advance_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.EXPORTING)

        # 4) export succeeds -> MP4 downloaded and stored -> READY
        client = FakeVideoGenClient(
            export_states=[succeeded(downloadUrl="https://signed.example/v.mp4")],
        )
        advance_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.READY)
        self.assertEqual(job.progress_percentage, 100)
        self.assertTrue(job.video_file)
        self.assertEqual(client.download_calls, ["https://signed.example/v.mp4"])
        self.assertEqual(job.video_file.read(), b"FAKE-MP4-BYTES")

    def test_generation_failure_fails_job(self):
        job = self._start()
        client = FakeVideoGenClient(run_states=[failed("generation exploded")])
        advance_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertIn("generation exploded", job.error)

    def test_export_start_failure_fails_job_without_retry(self):
        job = self._start()
        client = FakeVideoGenClient(
            run_states=[succeeded(projectId="pr_test")],
            export_error=BoomError("export refused"),
        )
        advance_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertIn("export refused", job.error)

    def test_transient_download_failure_releases_for_retry(self):
        job = self._start()
        # generation done, export started
        advance_job(
            job, client=FakeVideoGenClient(run_states=[succeeded(projectId="pr_test")])
        )
        job.refresh_from_db()

        # export succeeded but download blows up transiently
        from bakerydemo.videogen.exceptions import VideoGenConnectionError

        client = FakeVideoGenClient(
            export_states=[succeeded(downloadUrl="https://signed.example/v.mp4")],
            download_error=VideoGenConnectionError("net down"),
        )
        advance_job(job, client=client)
        job.refresh_from_db()
        # Not failed: released back to EXPORTING so a later poll can retry.
        self.assertEqual(job.status, JobStatus.EXPORTING)

        # A later poll finds the export ready again and stores it.
        client = FakeVideoGenClient(
            export_states=[succeeded(downloadUrl="https://signed.example/v.mp4")],
        )
        advance_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.READY)

    def test_transient_poll_error_keeps_job_active(self):
        from bakerydemo.videogen.exceptions import VideoGenRateLimitError

        job = self._start()

        class Boom(FakeVideoGenClient):
            def get_workflow_run(self, workflow_run_id):
                raise VideoGenRateLimitError("slow down", status=429)

        advance_job(job, client=Boom())
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.GENERATING)
