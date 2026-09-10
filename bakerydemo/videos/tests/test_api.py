from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos.models import VideoJob

User = get_user_model()


class VideoApiTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.objects.get(depth=1)
        cls.index = root.add_child(instance=BlogIndexPage(title="Blog", slug="blog-x"))
        cls.article = cls.index.add_child(
            instance=BlogPage(
                title="Tracking Wild Yeast",
                slug="twy-x",
                introduction="Yeasts are single-celled fungi. They live everywhere.",
            )
        )

        cls.superuser = User.objects.create_superuser("su", "su@example.com", "pw")
        cls.plain = User.objects.create_user("plain", "plain@example.com", "pw")
        cls.inactive = User.objects.create_superuser("gone", "gone@example.com", "pw")
        cls.inactive.is_active = False
        cls.inactive.save()

        _, cls.su_token = APIToken.create_token(user=cls.superuser, name="su")
        _, cls.plain_token = APIToken.create_token(user=cls.plain, name="plain")
        _, cls.inactive_token = APIToken.create_token(user=cls.inactive, name="gone")
        revoked, cls.revoked_token = APIToken.create_token(
            user=cls.superuser, name="revoked"
        )
        revoked.revoke()

    def start_url(self, page_id):
        return f"/api/v3-preview/pages/{page_id}/video/"

    def status_url(self, page_id, job_id):
        return f"/api/v3-preview/pages/{page_id}/video/{job_id}/"

    def auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class StartVideoAuthTests(VideoApiTestCase):
    def test_missing_token_is_unauthorized(self):
        resp = self.client.post(self.start_url(self.article.id))
        self.assertEqual(resp.status_code, 401)

    def test_revoked_token_is_unauthorized(self):
        resp = self.client.post(
            self.start_url(self.article.id), **self.auth(self.revoked_token)
        )
        self.assertEqual(resp.status_code, 401)

    def test_inactive_user_token_is_unauthorized(self):
        resp = self.client.post(
            self.start_url(self.article.id), **self.auth(self.inactive_token)
        )
        self.assertEqual(resp.status_code, 401)

    def test_user_without_publish_permission_is_forbidden(self):
        resp = self.client.post(
            self.start_url(self.article.id), **self.auth(self.plain_token)
        )
        self.assertEqual(resp.status_code, 403)


class StartVideoTests(VideoApiTestCase):
    def setUp(self):
        patcher = mock.patch("bakerydemo.videos.service._start_pipeline")
        self.start_pipeline = patcher.start()
        self.addCleanup(patcher.stop)

    def test_publisher_starts_job_and_gets_job_id(self):
        resp = self.client.post(
            self.start_url(self.article.id), **self.auth(self.su_token)
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("videoJobId", body)
        self.assertTrue(VideoJob.objects.filter(pk=body["videoJobId"]).exists())
        self.start_pipeline.assert_called_once()

    def test_second_request_is_idempotent(self):
        first = self.client.post(
            self.start_url(self.article.id), **self.auth(self.su_token)
        ).json()
        second = self.client.post(
            self.start_url(self.article.id), **self.auth(self.su_token)
        ).json()
        self.assertEqual(first["videoJobId"], second["videoJobId"])
        self.assertEqual(VideoJob.objects.count(), 1)
        # Only the first request kicked off provider work.
        self.start_pipeline.assert_called_once()

    def test_non_blog_page_is_rejected(self):
        resp = self.client.post(
            self.start_url(self.index.id), **self.auth(self.su_token)
        )
        self.assertEqual(resp.status_code, 422)

    def test_unknown_page_is_not_found(self):
        resp = self.client.post(self.start_url(999999), **self.auth(self.su_token))
        self.assertEqual(resp.status_code, 404)


class StatusVideoTests(VideoApiTestCase):
    def test_ready_job_reports_download_url(self):
        job = VideoJob.objects.create(
            page=self.article,
            status=VideoJob.Status.READY,
            progress_percentage=100,
            download_url="https://videogen.example/final.mp4",
        )
        resp = self.client.get(
            self.status_url(self.article.id, job.id), **self.auth(self.su_token)
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["progressPercentage"], 100)
        self.assertEqual(body["downloadUrl"], "https://videogen.example/final.mp4")
        self.assertIsNone(body["error"])

    def test_failed_job_reports_error(self):
        job = VideoJob.objects.create(
            page=self.article,
            status=VideoJob.Status.FAILED,
            error="Workflow run ended with status 'failed'.",
        )
        resp = self.client.get(
            self.status_url(self.article.id, job.id), **self.auth(self.su_token)
        )
        body = resp.json()
        self.assertEqual(body["status"], "failed")
        self.assertIsNone(body["downloadUrl"])
        self.assertIn("failed", body["error"])

    def test_processing_job_reports_progress_and_no_url(self):
        job = VideoJob.objects.create(
            page=self.article,
            status=VideoJob.Status.PROCESSING,
            progress_percentage=42,
        )
        body = self.client.get(
            self.status_url(self.article.id, job.id), **self.auth(self.su_token)
        ).json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progressPercentage"], 42)
        self.assertIsNone(body["downloadUrl"])

    def test_unknown_job_is_not_found(self):
        resp = self.client.get(
            self.status_url(self.article.id, "00000000-0000-0000-0000-000000000000"),
            **self.auth(self.su_token),
        )
        self.assertEqual(resp.status_code, 404)

    def test_status_requires_publish_permission(self):
        job = VideoJob.objects.create(
            page=self.article, status=VideoJob.Status.PROCESSING
        )
        resp = self.client.get(
            self.status_url(self.article.id, job.id), **self.auth(self.plain_token)
        )
        self.assertEqual(resp.status_code, 403)
