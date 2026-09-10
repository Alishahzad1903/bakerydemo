from unittest import mock

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from bakerydemo.videos.models import ArticleVideo

from .factories import (
    TempMediaRootMixin,
    create_blog_article,
    create_user_with_token,
)

API = "/api/v3-preview"


def _auth(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@override_settings(VIDEOGEN_API_KEY="test-key")
class StartVideoEndpointTests(TestCase):
    def setUp(self):
        self.article = create_blog_article()
        self.page_id = self.article.pk
        _, self.admin_token = create_user_with_token(username="pub", is_superuser=True)
        _, self.plain_token = create_user_with_token(
            username="noperm", is_superuser=False
        )
        # Stop the background pipeline from actually running in these tests.
        patcher = mock.patch("bakerydemo.videos.services.launch_pipeline")
        self.launch = patcher.start()
        self.addCleanup(patcher.stop)

    def _url(self):
        return f"{API}/pages/{self.page_id}/video/"

    def test_requires_authentication(self):
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 401)

    def test_rejects_invalid_token(self):
        resp = self.client.post(self._url(), **_auth("wagtail_not_a_real_token"))
        self.assertEqual(resp.status_code, 401)

    def test_forbids_caller_without_publish_permission(self):
        resp = self.client.post(self._url(), **_auth(self.plain_token))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(ArticleVideo.objects.count(), 0)

    def test_publisher_starts_video_and_gets_job_id(self):
        resp = self.client.post(self._url(), **_auth(self.admin_token))
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("videoJobId", body)
        self.assertEqual(body["status"], "pending")
        self.assertIn("progressPercentage", body)
        self.launch.assert_called_once()
        self.assertEqual(ArticleVideo.objects.count(), 1)

    def test_second_request_is_idempotent(self):
        first = self.client.post(self._url(), **_auth(self.admin_token))
        second = self.client.post(self._url(), **_auth(self.admin_token))
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(ArticleVideo.objects.count(), 1)
        # Only the first request launched the (billable) pipeline.
        self.launch.assert_called_once()

    def test_404_for_non_blog_page(self):
        # depth-1 root page is not a BlogPage.
        from wagtail.models import Page

        root = Page.objects.get(depth=1)
        resp = self.client.post(
            f"{API}/pages/{root.pk}/video/", **_auth(self.admin_token)
        )
        self.assertEqual(resp.status_code, 404)

    def test_404_for_unpublished_article(self):
        draft = create_blog_article(live=False)
        resp = self.client.post(
            f"{API}/pages/{draft.pk}/video/", **_auth(self.admin_token)
        )
        self.assertEqual(resp.status_code, 404)


@override_settings(VIDEOGEN_API_KEY="test-key")
class VideoStatusEndpointTests(TempMediaRootMixin, TestCase):
    def setUp(self):
        self.article = create_blog_article()
        self.page_id = self.article.pk
        _, self.admin_token = create_user_with_token(username="pub", is_superuser=True)
        self.video = ArticleVideo.objects.create(
            page=self.article.page_ptr, script="Tracking Wild Yeast. Yeasts grow."
        )

    def _status_url(self, job_id=None):
        job_id = job_id or self.video.job_id
        return f"{API}/pages/{self.page_id}/video/{job_id}/"

    def test_status_requires_authentication(self):
        self.assertEqual(self.client.get(self._status_url()).status_code, 401)

    def test_pending_status_contract(self):
        resp = self.client.get(self._status_url(), **_auth(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["progressPercentage"], 0)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])
        self.assertEqual(body["videoJobId"], str(self.video.job_id))

    def test_failed_status_carries_error(self):
        self.video.mark_failed("it broke", code="render_error")
        body = self.client.get(self._status_url(), **_auth(self.admin_token)).json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "it broke")
        self.assertIsNone(body["downloadUrl"])

    def test_succeeded_status_carries_download_url(self):
        self.video.video_file.save("x.mp4", ContentFile(b"MP4DATA"), save=True)
        self.video.mark_succeeded()
        body = self.client.get(self._status_url(), **_auth(self.admin_token)).json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["progressPercentage"], 100)
        self.assertIsNotNone(body["downloadUrl"])
        self.assertTrue(body["downloadUrl"].endswith("/download/"))
        self.assertIsNone(body["error"])

    def test_unknown_job_id_is_404(self):
        import uuid

        resp = self.client.get(
            self._status_url(uuid.uuid4()), **_auth(self.admin_token)
        )
        self.assertEqual(resp.status_code, 404)

    def test_download_streams_mp4_when_ready(self):
        self.video.video_file.save("x.mp4", ContentFile(b"MP4DATA"), save=True)
        self.video.mark_succeeded()
        url = f"{self._status_url()}download/"
        resp = self.client.get(url, **_auth(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "video/mp4")
        self.assertEqual(b"".join(resp.streaming_content), b"MP4DATA")

    def test_download_404_when_not_ready(self):
        url = f"{self._status_url()}download/"
        resp = self.client.get(url, **_auth(self.admin_token))
        self.assertEqual(resp.status_code, 404)
