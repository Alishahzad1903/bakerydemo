import tempfile
from unittest import mock

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from wagtail.models import APIToken

from bakerydemo.videos.models import ArticleVideo, VideoStatus

from ._helpers import make_blog_article, make_token, make_user


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ArticleVideoApiTests(TestCase):
    def setUp(self):
        self.article = make_blog_article()
        self.publisher = make_user("pub", superuser=True)
        self.non_publisher = make_user("plain", superuser=False)
        self.pub_token = make_token(self.publisher)
        self.plain_token = make_token(self.non_publisher)
        self.base = f"/api/v3-preview/pages/{self.article.pk}/video/"

    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    # -- auth & permissions ---------------------------------------------------

    def test_post_requires_authentication(self):
        self.assertEqual(self.client.post(self.base).status_code, 401)

    def test_post_forbidden_for_non_publisher(self):
        resp = self.client.post(self.base, **self._auth(self.plain_token))
        self.assertEqual(resp.status_code, 403)

    def test_revoked_token_is_unauthorised(self):
        from django.utils import timezone

        token = make_token(self.publisher, name="revoked")
        APIToken.objects.filter(
            key_hash=APIToken.hash_token(token)
        ).update(revoked_at=timezone.now())
        self.assertEqual(
            self.client.post(self.base, **self._auth(token)).status_code, 401
        )

    # -- validation -----------------------------------------------------------

    def test_non_blog_page_is_rejected(self):
        from wagtail.models import Page

        root = Page.objects.filter(depth=1).first()
        self.assertEqual(
            self.client.post(
                f"/api/v3-preview/pages/{root.pk}/video/", **self._auth(self.pub_token)
            ).status_code,
            403,  # publishing the root is not permitted -> permission check first
        )

    # -- start & idempotency --------------------------------------------------

    def test_post_starts_production_and_is_idempotent(self):
        with mock.patch(
            "bakerydemo.videos.api.start_production_async"
        ) as start_async:
            first = self.client.post(self.base, **self._auth(self.pub_token))
            self.assertEqual(first.status_code, 202)
            job_id = first.json()["videoJobId"]
            start_async.assert_called_once()

            second = self.client.post(self.base, **self._auth(self.pub_token))
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json()["videoJobId"], job_id)
            # Still only one production run kicked off, one row in the DB.
            start_async.assert_called_once()
            self.assertEqual(ArticleVideo.objects.filter(page=self.article).count(), 1)

    def test_failed_job_allows_a_fresh_attempt(self):
        ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.FAILED, error="nope"
        )
        with mock.patch("bakerydemo.videos.api.start_production_async"):
            resp = self.client.post(self.base, **self._auth(self.pub_token))
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(ArticleVideo.objects.filter(page=self.article).count(), 2)

    # -- status ---------------------------------------------------------------

    def test_status_contract_fields(self):
        av = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.PROCESSING, progress_percentage=42
        )
        resp = self.client.get(
            f"{self.base}{av.job_id}/", **self._auth(self.pub_token)
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(
            set(data),
            {"videoJobId", "status", "progressPercentage", "downloadUrl", "error"},
        )
        self.assertEqual(data["status"], "processing")
        self.assertEqual(data["progressPercentage"], 42)
        self.assertIsNone(data["downloadUrl"])

    def test_status_carries_download_url_when_ready(self):
        av = ArticleVideo.objects.create(page=self.article, status=VideoStatus.READY)
        av.video_file.save("v.mp4", ContentFile(b"\x00\x00\x00\x20ftypisom"), save=True)
        data = self.client.get(
            f"{self.base}{av.job_id}/", **self._auth(self.pub_token)
        ).json()
        self.assertIn(f"/video/{av.job_id}/download/", data["downloadUrl"])

    def test_status_carries_error_when_failed(self):
        av = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.FAILED, error="boom"
        )
        data = self.client.get(
            f"{self.base}{av.job_id}/", **self._auth(self.pub_token)
        ).json()
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], "boom")

    def test_unknown_job_is_404(self):
        resp = self.client.get(
            f"{self.base}00000000-0000-0000-0000-000000000000/",
            **self._auth(self.pub_token),
        )
        self.assertEqual(resp.status_code, 404)

    # -- download -------------------------------------------------------------

    def test_download_conflict_when_not_ready(self):
        av = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.PROCESSING
        )
        resp = self.client.get(
            f"{self.base}{av.job_id}/download/", **self._auth(self.pub_token)
        )
        self.assertEqual(resp.status_code, 409)

    def test_download_streams_mp4_when_ready(self):
        av = ArticleVideo.objects.create(page=self.article, status=VideoStatus.READY)
        av.video_file.save("v.mp4", ContentFile(b"\x00\x00\x00\x20ftypisom"), save=True)
        resp = self.client.get(
            f"{self.base}{av.job_id}/download/", **self._auth(self.pub_token)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "video/mp4")
        self.assertIn("attachment", resp["Content-Disposition"])

    def test_download_forbidden_for_non_publisher(self):
        av = ArticleVideo.objects.create(page=self.article, status=VideoStatus.READY)
        av.video_file.save("v.mp4", ContentFile(b"data"), save=True)
        resp = self.client.get(
            f"{self.base}{av.job_id}/download/", **self._auth(self.plain_token)
        )
        self.assertEqual(resp.status_code, 403)
