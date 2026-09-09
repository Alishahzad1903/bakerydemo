from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase
from django.utils import timezone
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos.models import ArticleVideo, VideoStatus


class VideoApiTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        cls.index = BlogIndexPage(title="Blog", slug="blog", introduction="")
        root.add_child(instance=cls.index)

        cls.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="tracking-wild-yeast",
            introduction="Yeasts grow as single cells. They are fungi.",
        )
        cls.index.add_child(instance=cls.article)
        cls.article.save_revision().publish()

        cls.draft = BlogPage(
            title="Unpublished Loaf",
            slug="unpublished-loaf",
            introduction="A draft. Second sentence.",
            live=False,
        )
        cls.index.add_child(instance=cls.draft)

        # A caller allowed to publish (superuser), and one who is not.
        cls.publisher = User.objects.create_superuser(
            "publisher", "pub@example.com", "pw"
        )
        cls.plain = User.objects.create_user("plain", "plain@example.com", "pw")
        cls.inactive = User.objects.create_superuser(
            "inactive_user", "inactive@example.com", "pw"
        )
        cls.inactive.is_active = False
        cls.inactive.save()

        _, cls.publisher_token = APIToken.create_token(
            user=cls.publisher, name="pub"
        )
        _, cls.plain_token = APIToken.create_token(user=cls.plain, name="plain")
        _, cls.inactive_token = APIToken.create_token(
            user=cls.inactive, name="inactive"
        )
        revoked, cls.revoked_token = APIToken.create_token(
            user=cls.publisher, name="revoked"
        )
        revoked.revoked_at = timezone.now()
        revoked.save()

    def start_url(self, page_id=None):
        return f"/api/v3-preview/pages/{page_id or self.article.pk}/video/"

    def auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class StartVideoPermissionTests(VideoApiTestBase):
    def test_requires_authentication(self):
        response = self.client.post(self.start_url())
        self.assertEqual(response.status_code, 401)

    def test_forbidden_for_non_publisher(self):
        response = self.client.post(self.start_url(), **self.auth(self.plain_token))
        self.assertEqual(response.status_code, 403)

    def test_inactive_user_rejected(self):
        response = self.client.post(self.start_url(), **self.auth(self.inactive_token))
        self.assertEqual(response.status_code, 401)

    def test_revoked_token_rejected(self):
        response = self.client.post(self.start_url(), **self.auth(self.revoked_token))
        self.assertEqual(response.status_code, 401)


class StartVideoTests(VideoApiTestBase):
    @patch("bakerydemo.videos.service.enqueue_production")
    def test_publisher_starts_and_is_idempotent(self, mock_enqueue):
        response = self.client.post(
            self.start_url(), **self.auth(self.publisher_token)
        )
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["videoJobId"]
        self.assertTrue(job_id)

        # Second request: same job, no new production, still 1 row.
        again = self.client.post(self.start_url(), **self.auth(self.publisher_token))
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["videoJobId"], job_id)
        mock_enqueue.assert_called_once()
        self.assertEqual(ArticleVideo.objects.filter(page=self.article).count(), 1)

    @patch("bakerydemo.videos.service.enqueue_production")
    def test_non_blog_page_not_found(self, mock_enqueue):
        response = self.client.post(
            self.start_url(self.index.pk), **self.auth(self.publisher_token)
        )
        self.assertEqual(response.status_code, 404)
        mock_enqueue.assert_not_called()

    @patch("bakerydemo.videos.service.enqueue_production")
    def test_draft_article_conflict(self, mock_enqueue):
        response = self.client.post(
            self.start_url(self.draft.pk), **self.auth(self.publisher_token)
        )
        self.assertEqual(response.status_code, 409)
        mock_enqueue.assert_not_called()


class VideoStatusTests(VideoApiTestBase):
    def status_url(self, job_id, page_id=None):
        pid = page_id or self.article.pk
        return f"/api/v3-preview/pages/{pid}/video/{job_id}/"

    def test_reports_processing(self):
        video = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.PROCESSING, progress=42
        )
        response = self.client.get(
            self.status_url(video.job_id), **self.auth(self.publisher_token)
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["videoJobId"], str(video.job_id))
        self.assertEqual(data["status"], "processing")
        self.assertEqual(data["progressPercentage"], 42)
        self.assertIsNone(data["downloadUrl"])
        self.assertIsNone(data["error"])

    def test_reports_ready_with_download_url(self):
        video = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.SUCCEEDED, progress=100
        )
        video.mp4.save("v.mp4", ContentFile(b"mp4-bytes"), save=True)

        response = self.client.get(
            self.status_url(video.job_id), **self.auth(self.publisher_token)
        )
        data = response.json()
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["progressPercentage"], 100)
        self.assertTrue(data["downloadUrl"].endswith(f"/{video.job_id}/download/"))
        self.assertIsNone(data["error"])

    def test_reports_failure_with_error(self):
        video = ArticleVideo.objects.create(
            page=self.article,
            status=VideoStatus.FAILED,
            error="no footage (not_found)",
        )
        response = self.client.get(
            self.status_url(video.job_id), **self.auth(self.publisher_token)
        )
        data = response.json()
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], "no footage (not_found)")
        self.assertIsNone(data["downloadUrl"])

    def test_status_forbidden_for_non_publisher(self):
        video = ArticleVideo.objects.create(page=self.article)
        response = self.client.get(
            self.status_url(video.job_id), **self.auth(self.plain_token)
        )
        self.assertEqual(response.status_code, 403)

    def test_unknown_job_id_404(self):
        response = self.client.get(
            self.status_url("00000000-0000-0000-0000-000000000000"),
            **self.auth(self.publisher_token),
        )
        self.assertEqual(response.status_code, 404)


class VideoDownloadTests(VideoApiTestBase):
    def download_url(self, job_id):
        return f"/api/v3-preview/pages/{self.article.pk}/video/{job_id}/download/"

    def test_downloads_ready_video(self):
        video = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.SUCCEEDED, progress=100
        )
        video.mp4.save("v.mp4", ContentFile(b"mp4-bytes"), save=True)

        response = self.client.get(
            self.download_url(video.job_id), **self.auth(self.publisher_token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertEqual(b"".join(response.streaming_content), b"mp4-bytes")

    def test_download_not_ready_404(self):
        video = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.PROCESSING
        )
        response = self.client.get(
            self.download_url(video.job_id), **self.auth(self.publisher_token)
        )
        self.assertEqual(response.status_code, 404)

    def test_download_requires_permission(self):
        video = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.SUCCEEDED, progress=100
        )
        video.mp4.save("v.mp4", ContentFile(b"mp4-bytes"), save=True)
        response = self.client.get(
            self.download_url(video.job_id), **self.auth(self.plain_token)
        )
        self.assertEqual(response.status_code, 403)
