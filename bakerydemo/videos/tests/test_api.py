from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos.models import VideoJob, VideoJobStatus

User = get_user_model()

PATCH_START = "bakerydemo.videos.api.start_production"


class VideoApiTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        cls.index = BlogIndexPage(title="Blog", slug="blog-test")
        root.add_child(instance=cls.index)
        cls.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="twy-test",
            introduction="Yeasts can be contrasted with molds. A second sentence.",
        )
        cls.index.add_child(instance=cls.article)

        cls.publisher = User.objects.create(
            username="pub", is_active=True, is_superuser=True, is_staff=True
        )
        cls.viewer = User.objects.create(username="view", is_active=True)

        _, cls.pub_token = APIToken.create_token(user=cls.publisher, name="pub")
        _, cls.view_token = APIToken.create_token(user=cls.viewer, name="view")
        revoked, cls.revoked_token = APIToken.create_token(
            user=cls.publisher, name="rev"
        )
        revoked.revoke()

    def _post(self, page_id, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(f"/api/v3-preview/pages/{page_id}/video/", **headers)

    def _get(self, page_id, job_id, token=None, suffix=""):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(
            f"/api/v3-preview/pages/{page_id}/video/{job_id}/{suffix}", **headers
        )


class AuthTests(VideoApiTestCase):
    def test_post_requires_token(self):
        self.assertEqual(self._post(self.article.id).status_code, 401)

    def test_post_forbidden_for_non_publisher(self):
        with mock.patch(PATCH_START):
            resp = self._post(self.article.id, self.view_token)
        self.assertEqual(resp.status_code, 403)

    def test_post_rejects_revoked_token(self):
        self.assertEqual(
            self._post(self.article.id, self.revoked_token).status_code, 401
        )

    def test_missing_page_is_404(self):
        self.assertEqual(self._post(999999, self.pub_token).status_code, 404)

    def test_non_blog_page_is_400(self):
        with mock.patch(PATCH_START):
            resp = self._post(self.index.id, self.pub_token)
        self.assertEqual(resp.status_code, 400)


class ProductionTests(VideoApiTestCase):
    def test_publisher_starts_job(self):
        with mock.patch(PATCH_START) as start:
            resp = self._post(self.article.id, self.pub_token)
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("videoJobId", body)
        self.assertEqual(body["status"], VideoJobStatus.PENDING)
        self.assertEqual(body["progressPercentage"], 0)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        start.assert_called_once()
        job = VideoJob.objects.get(page=self.article)
        # Narration is built from the article's own words.
        self.assertTrue(job.script.startswith("Tracking Wild Yeast."))

    def test_repeat_request_is_idempotent(self):
        with mock.patch(PATCH_START) as start:
            first = self._post(self.article.id, self.pub_token)
            second = self._post(self.article.id, self.pub_token)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        start.assert_called_once()


class StatusAndDownloadTests(VideoApiTestCase):
    def _make_job(self, **kwargs):
        defaults = {"page": self.article, "script": "Tracking Wild Yeast."}
        defaults.update(kwargs)
        return VideoJob.objects.create(**defaults)

    def test_status_returns_job(self):
        job = self._make_job(status=VideoJobStatus.PROCESSING, progress_percentage=42)
        resp = self._get(self.article.id, job.id, self.pub_token)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["videoJobId"], str(job.id))
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progressPercentage"], 42)
        self.assertIsNone(body["downloadUrl"])

    def test_status_unknown_job_is_404(self):
        resp = self._get(
            self.article.id, "00000000-0000-0000-0000-000000000000", self.pub_token
        )
        self.assertEqual(resp.status_code, 404)

    def test_status_forbidden_for_non_publisher(self):
        job = self._make_job()
        self.assertEqual(
            self._get(self.article.id, job.id, self.view_token).status_code, 403
        )

    def test_ready_job_exposes_download_url(self):
        job = self._make_job(
            status=VideoJobStatus.READY,
            progress_percentage=100,
            project_id="vg_proj_x",
            export_id="vg_expo_x",
        )
        resp = self._get(self.article.id, job.id, self.pub_token)
        body = resp.json()
        self.assertEqual(body["status"], "ready")
        self.assertIsNotNone(body["downloadUrl"])
        self.assertTrue(body["downloadUrl"].endswith(f"/video/{job.id}/download/"))

    def test_download_redirects_when_ready(self):
        job = self._make_job(
            status=VideoJobStatus.READY,
            progress_percentage=100,
            project_id="vg_proj_x",
            export_id="vg_expo_x",
        )
        with mock.patch(
            "bakerydemo.videos.api.refresh_download_url",
            return_value="https://signed.example/video.mp4",
        ):
            resp = self._get(
                self.article.id, job.id, self.pub_token, suffix="download/"
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "https://signed.example/video.mp4")

    def test_download_conflict_when_not_ready(self):
        job = self._make_job(status=VideoJobStatus.PROCESSING)
        resp = self._get(self.article.id, job.id, self.pub_token, suffix="download/")
        self.assertEqual(resp.status_code, 409)
