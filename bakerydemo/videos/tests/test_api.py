"""End-to-end API tests: auth, permissions, idempotency, status and download."""

from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from wagtail.models import APIToken

from bakerydemo.videos.constants import VideoJobStatus
from bakerydemo.videos.models import VideoJob

from .factories import bearer, build_article, make_token

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class ArticleVideoAPITestBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="videos-test-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.article = build_article()
        self.publisher = User.objects.create_superuser("pub", "pub@example.com", "pw")
        self.non_publisher = User.objects.create_user(
            "plain", "plain@example.com", "pw"
        )
        self.publisher_token = make_token(self.publisher)
        self.non_publisher_token = make_token(self.non_publisher)

    def post_video(self, page_id=None, token=None, **extra):
        page_id = page_id if page_id is not None else self.article.pk
        headers = bearer(token) if token else {}
        return self.client.post(
            f"/api/v3-preview/pages/{page_id}/video/", **headers, **extra
        )

    def get_video(self, job_id, page_id=None, token=None):
        page_id = page_id if page_id is not None else self.article.pk
        headers = bearer(token) if token else {}
        return self.client.get(
            f"/api/v3-preview/pages/{page_id}/video/{job_id}/", **headers
        )


class AuthAndPermissionTests(ArticleVideoAPITestBase):
    def test_publisher_can_start(self):
        with mock.patch("bakerydemo.videos.services.start_job_thread"):
            resp = self.post_video(token=self.publisher_token)
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("videoJobId", body)
        self.assertEqual(body["status"], "processing")

    def test_non_publisher_is_forbidden(self):
        resp = self.post_video(token=self.non_publisher_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(VideoJob.objects.count(), 0)

    def test_missing_token_is_unauthorized(self):
        resp = self.post_video(token=None)
        self.assertEqual(resp.status_code, 401)

    def test_garbage_token_is_unauthorized(self):
        resp = self.post_video(token="wagtail_not_a_real_token")
        self.assertEqual(resp.status_code, 401)

    def test_revoked_token_is_unauthorized(self):
        instance, plaintext = APIToken.create_token(user=self.publisher, name="revoked")
        instance.revoke()
        resp = self.post_video(token=plaintext)
        self.assertEqual(resp.status_code, 401)

    def test_inactive_user_is_unauthorized(self):
        inactive = User.objects.create_superuser("ghost", "ghost@example.com", "pw")
        token = make_token(inactive)
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        resp = self.post_video(token=token)
        self.assertEqual(resp.status_code, 401)


class ResourceResolutionTests(ArticleVideoAPITestBase):
    def test_unknown_page_is_404(self):
        resp = self.post_video(page_id=999999, token=self.publisher_token)
        self.assertEqual(resp.status_code, 404)

    def test_non_blog_page_is_400(self):
        from wagtail.models import Page

        root = Page.get_first_root_node()
        resp = self.post_video(page_id=root.pk, token=self.publisher_token)
        self.assertEqual(resp.status_code, 400)

    def test_draft_article_not_startable(self):
        draft = build_article(slug="draft-article", live=False)
        resp = self.post_video(page_id=draft.pk, token=self.publisher_token)
        self.assertEqual(resp.status_code, 404)


class IdempotencyTests(ArticleVideoAPITestBase):
    def test_second_post_returns_same_job_without_new_video(self):
        with mock.patch("bakerydemo.videos.services.start_job_thread") as runner:
            # captureOnCommitCallbacks lets the post-commit runner actually fire
            # inside the test transaction.
            with self.captureOnCommitCallbacks(execute=True):
                first = self.post_video(token=self.publisher_token)
            with self.captureOnCommitCallbacks(execute=True):
                second = self.post_video(token=self.publisher_token)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(VideoJob.objects.count(), 1)
        # The pipeline is started exactly once — the repeat request produces no
        # second video and no second charge.
        self.assertEqual(runner.call_count, 1)


class StatusAndDownloadTests(ArticleVideoAPITestBase):
    def _make_job(self, **kwargs):
        return VideoJob.objects.create(page=self.article, **kwargs)

    def test_processing_status(self):
        job = self._make_job(status=VideoJobStatus.PROCESSING, progress_percentage=42)
        resp = self.get_video(job.pk, token=self.publisher_token)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progressPercentage"], 42)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])

    def test_failed_status_carries_error(self):
        job = self._make_job(status=VideoJobStatus.FAILED, error_message="it broke")
        resp = self.get_video(job.pk, token=self.publisher_token)
        body = resp.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "it broke")
        self.assertIsNone(body["downloadUrl"])

    def test_ready_status_and_download(self):
        job = self._make_job(status=VideoJobStatus.PROCESSING)
        job.video_file.save("v.mp4", ContentFile(b"FINISHEDMP4"), save=False)
        job.status = VideoJobStatus.SUCCEEDED
        job.progress_percentage = 100
        job.save()

        resp = self.get_video(job.pk, token=self.publisher_token)
        body = resp.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["progressPercentage"], 100)
        self.assertTrue(body["downloadUrl"].endswith(f"/video/{job.pk}/download/"))

        # Download the finished MP4 through the site endpoint.
        dl = self.client.get(
            f"/api/v3-preview/pages/{self.article.pk}/video/{job.pk}/download/",
            **bearer(self.publisher_token),
        )
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl["Content-Type"], "video/mp4")
        self.assertEqual(b"".join(dl.streaming_content), b"FINISHEDMP4")

    def test_download_before_ready_is_conflict(self):
        job = self._make_job(status=VideoJobStatus.PROCESSING)
        dl = self.client.get(
            f"/api/v3-preview/pages/{self.article.pk}/video/{job.pk}/download/",
            **bearer(self.publisher_token),
        )
        self.assertEqual(dl.status_code, 409)

    def test_job_of_other_page_is_404(self):
        other = build_article(slug="other-article")
        job = self._make_job(status=VideoJobStatus.PROCESSING)
        resp = self.get_video(job.pk, page_id=other.pk, token=self.publisher_token)
        self.assertEqual(resp.status_code, 404)

    def test_status_requires_publish_permission(self):
        job = self._make_job(status=VideoJobStatus.PROCESSING)
        resp = self.get_video(job.pk, token=self.non_publisher_token)
        self.assertEqual(resp.status_code, 403)
