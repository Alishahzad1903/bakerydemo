from unittest import mock

from bakerydemo.videogen.models import JobStatus, VideoJob

from .base import BakeryVideoTestCase
from .fakes import FakeVideoGenClient, succeeded

CLIENT_PATH = "bakerydemo.videogen.service.get_client"


class VideoAPIAuthTests(BakeryVideoTestCase):
    def url(self):
        return f"/api/v3-preview/pages/{self.article.pk}/video/"

    def test_missing_token_is_401(self):
        resp = self.client.post(self.url())
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_is_401(self):
        resp = self.client.post(self.url(), **self.auth("wagtail_not_a_real_token"))
        self.assertEqual(resp.status_code, 401)

    def test_inactive_user_is_401(self):
        resp = self.client.post(self.url(), **self.auth(self.inactive_token))
        self.assertEqual(resp.status_code, 401)

    def test_non_publisher_is_403(self):
        resp = self.client.post(self.url(), **self.auth(self.plain_token))
        self.assertEqual(resp.status_code, 403)

    def test_unknown_page_is_404(self):
        resp = self.client.post(
            "/api/v3-preview/pages/999999/video/", **self.auth(self.super_token)
        )
        self.assertEqual(resp.status_code, 404)

    def test_non_blog_page_is_404(self):
        resp = self.client.post(
            f"/api/v3-preview/pages/{self.index.pk}/video/",
            **self.auth(self.super_token),
        )
        self.assertEqual(resp.status_code, 404)


class VideoAPIFlowTests(BakeryVideoTestCase):
    def create_url(self):
        return f"/api/v3-preview/pages/{self.article.pk}/video/"

    def status_url(self, job_id):
        return f"/api/v3-preview/pages/{self.article.pk}/video/{job_id}/"

    def download_url(self, job_id):
        return f"/api/v3-preview/pages/{self.article.pk}/video/{job_id}/download/"

    def test_create_returns_job_id(self):
        fake = FakeVideoGenClient()
        with mock.patch(CLIENT_PATH, return_value=fake):
            resp = self.client.post(self.create_url(), **self.auth(self.super_token))
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("videoJobId", body)
        self.assertEqual(VideoJob.objects.count(), 1)
        self.assertEqual(body["videoJobId"], VideoJob.objects.get().pk)

    def test_create_is_idempotent(self):
        fake = FakeVideoGenClient()
        with mock.patch(CLIENT_PATH, return_value=fake):
            first = self.client.post(self.create_url(), **self.auth(self.super_token))
            second = self.client.post(self.create_url(), **self.auth(self.super_token))
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(VideoJob.objects.count(), 1)
        self.assertEqual(len(fake.create_calls), 1)

    def test_status_progresses_to_ready_and_downloads(self):
        fake = FakeVideoGenClient(
            run_states=[succeeded(projectId="pr_test")],
            export_states=[succeeded(downloadUrl="https://signed.example/v.mp4")],
        )
        with mock.patch(CLIENT_PATH, return_value=fake):
            created = self.client.post(self.create_url(), **self.auth(self.super_token))
            job_id = created.json()["videoJobId"]

            # First poll: generation succeeds, export starts -> processing
            r1 = self.client.get(self.status_url(job_id), **self.auth(self.super_token))
            self.assertEqual(r1.status_code, 200)
            b1 = r1.json()
            self.assertEqual(b1["status"], "processing")
            self.assertIsNone(b1["downloadUrl"])
            self.assertIsNone(b1["error"])
            self.assertIsInstance(b1["progressPercentage"], int)

            # Second poll: export succeeds, MP4 stored -> ready
            r2 = self.client.get(self.status_url(job_id), **self.auth(self.super_token))
            b2 = r2.json()
            self.assertEqual(b2["status"], "ready")
            self.assertEqual(b2["progressPercentage"], 100)
            self.assertTrue(b2["downloadUrl"])
            self.assertIn(f"/video/{job_id}/download/", b2["downloadUrl"])

            # Download the finished MP4 through the site.
            dl = self.client.get(
                self.download_url(job_id), **self.auth(self.super_token)
            )
            self.assertEqual(dl.status_code, 200)
            self.assertEqual(dl["Content-Type"], "video/mp4")
            self.assertEqual(b"".join(dl.streaming_content), b"FAKE-MP4-BYTES")

    def test_status_unknown_job_is_404(self):
        resp = self.client.get(self.status_url(424242), **self.auth(self.super_token))
        self.assertEqual(resp.status_code, 404)

    def test_download_before_ready_is_404(self):
        fake = FakeVideoGenClient()
        with mock.patch(CLIENT_PATH, return_value=fake):
            created = self.client.post(self.create_url(), **self.auth(self.super_token))
            job_id = created.json()["videoJobId"]
            resp = self.client.get(
                self.download_url(job_id), **self.auth(self.super_token)
            )
        self.assertEqual(resp.status_code, 404)

    def test_status_requires_publish_permission(self):
        fake = FakeVideoGenClient()
        with mock.patch(CLIENT_PATH, return_value=fake):
            created = self.client.post(self.create_url(), **self.auth(self.super_token))
            job_id = created.json()["videoJobId"]
        resp = self.client.get(self.status_url(job_id), **self.auth(self.plain_token))
        self.assertEqual(resp.status_code, 403)

    def test_failed_job_reports_error(self):
        job = VideoJob.objects.create(
            page=self.article, status=JobStatus.FAILED, error="it broke"
        )
        resp = self.client.get(self.status_url(job.pk), **self.auth(self.super_token))
        body = resp.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "it broke")
        self.assertIsNone(body["downloadUrl"])
