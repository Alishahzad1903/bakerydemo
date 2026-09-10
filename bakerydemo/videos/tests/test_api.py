"""HTTP-layer tests — routing, bearer auth, and the publish-permission gate.

The provider/orchestration is mocked here; this file is about who the endpoints
admit and refuse, and the exact response shape. The full seeded-token matrix is
also verified live during self-verification.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos.models import ArticleVideo

User = get_user_model()


class VideoApiTestBase(TestCase):
    def setUp(self):
        root = Page.objects.filter(depth=1).first()
        self.index = BlogIndexPage(title="Blog", slug="blog-api-test")
        root.add_child(instance=self.index)
        self.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="twy-api-test",
            introduction="Yeasts are fascinating.",
            live=True,
        )
        self.index.add_child(instance=self.article)

        self.superuser = User.objects.create_user(
            username="pub", password="x", is_superuser=True, is_staff=True
        )
        self.plain = User.objects.create_user(username="noperm", password="x")

    def _token(self, user):
        _, plaintext = APIToken.create_token(user=user, name=f"tok-{user.username}")
        return plaintext

    def _post(self, page_id, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(f"/api/v3-preview/pages/{page_id}/video/", **headers)

    def _get(self, page_id, job_id, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(
            f"/api/v3-preview/pages/{page_id}/video/{job_id}/", **headers
        )


class AuthAndPermissionTests(VideoApiTestBase):
    def test_unauthenticated_is_401(self):
        self.assertEqual(self._post(self.article.pk).status_code, 401)

    def test_revoked_token_is_401(self):
        token = self._token(self.superuser)
        APIToken.objects.filter(user=self.superuser).update(
            revoked_at="2020-01-01T00:00:00Z"
        )
        self.assertEqual(self._post(self.article.pk, token).status_code, 401)

    def test_user_without_publish_permission_is_403(self):
        token = self._token(self.plain)
        self.assertEqual(self._post(self.article.pk, token).status_code, 403)

    @mock.patch("bakerydemo.videos.api.service.request_video")
    def test_publisher_starts_job_and_gets_video_job_id(self, mock_request):
        job = ArticleVideo.objects.create(
            page=self.article, status=ArticleVideo.Status.PROCESSING
        )
        mock_request.return_value = job

        resp = self._post(self.article.pk, self._token(self.superuser))

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json(), {"videoJobId": str(job.job_id)})
        mock_request.assert_called_once()


class RequestValidationTests(VideoApiTestBase):
    def test_non_blog_page_is_400(self):
        # The blog index itself is a published Page but not a BlogPage article.
        resp = self._post(self.index.pk, self._token(self.superuser))
        self.assertEqual(resp.status_code, 400)

    def test_unknown_page_is_404(self):
        self.assertEqual(self._post(999999, self._token(self.superuser)).status_code, 404)


class StatusEndpointTests(VideoApiTestBase):
    def test_processing_job_reports_top_level_fields(self):
        job = ArticleVideo.objects.create(
            page=self.article,
            status=ArticleVideo.Status.PROCESSING,
            progress_percentage=42.5,
        )
        resp = self._get(self.article.pk, job.job_id, self._token(self.superuser))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["videoJobId"], str(job.job_id))
        self.assertEqual(data["status"], "processing")
        self.assertEqual(data["progressPercentage"], 42.5)
        self.assertIsNone(data["downloadUrl"])
        self.assertIsNone(data["error"])

    @mock.patch(
        "bakerydemo.videos.api.service.fresh_download_url",
        return_value="https://videogen.example/out.mp4",
    )
    def test_ready_job_carries_download_url(self, _mock_url):
        job = ArticleVideo.objects.create(
            page=self.article,
            status=ArticleVideo.Status.READY,
            progress_percentage=100.0,
            download_url="https://videogen.example/out.mp4",
        )
        resp = self._get(self.article.pk, job.job_id, self._token(self.superuser))
        data = resp.json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["downloadUrl"], "https://videogen.example/out.mp4")

    def test_failed_job_carries_error(self):
        job = ArticleVideo.objects.create(
            page=self.article,
            status=ArticleVideo.Status.FAILED,
            error="VideoGen build failed: boom",
        )
        resp = self._get(self.article.pk, job.job_id, self._token(self.superuser))
        data = resp.json()
        self.assertEqual(data["status"], "failed")
        self.assertIn("boom", data["error"])

    def test_unknown_job_id_is_404(self):
        resp = self._get(
            self.article.pk,
            "11111111-1111-1111-1111-111111111111",
            self._token(self.superuser),
        )
        self.assertEqual(resp.status_code, 404)
