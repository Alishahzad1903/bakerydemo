from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from bakerydemo.video import services
from bakerydemo.video.models import ArticleVideo, VideoStatus

from .utils import create_blog_article, make_user, token_for


def start_url(page_id) -> str:
    return reverse("wagtailapi_v3:pages_video_start", kwargs={"page_id": page_id})


def status_url(page_id, job_id) -> str:
    return reverse(
        "wagtailapi_v3:pages_video_status",
        kwargs={"page_id": page_id, "video_job_id": job_id},
    )


def bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@override_settings(VIDEOGEN_API_KEY="sk_test_dummy")
class StartVideoAuthTests(TestCase):
    """The permission matrix, mirroring the seeded-token users."""

    @classmethod
    def setUpTestData(cls):
        cls.article = create_blog_article()

    def setUp(self):
        # No real threads, no real VideoGen client.
        self.spawn = mock.patch.object(services, "spawn_worker").start()
        self.get_client = mock.patch.object(services, "get_client").start()
        self.addCleanup(mock.patch.stopall)

    def _post(self, token=None):
        headers = bearer(token) if token else {}
        return self.client.post(
            start_url(self.article.id), content_type="application/json", **headers
        )

    def test_superuser_allowed(self):
        token = token_for(make_user("boss", superuser=True))
        resp = self._post(token)
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("videoJobId", body)
        self.assertTrue(ArticleVideo.objects.filter(pk=body["videoJobId"]).exists())
        self.spawn.assert_called_once()

    def test_moderator_allowed(self):
        token = token_for(make_user("mod", group="Moderators"))
        self.assertEqual(self._post(token).status_code, 202)

    def test_editor_forbidden(self):
        token = token_for(make_user("ed", group="Editors"))
        resp = self._post(token)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ArticleVideo.objects.exists())
        self.spawn.assert_not_called()

    def test_authenticated_without_publish_forbidden(self):
        token = token_for(make_user("plain"))
        self.assertEqual(self._post(token).status_code, 403)

    def test_missing_token_unauthorized(self):
        self.assertEqual(self._post().status_code, 401)

    def test_invalid_token_unauthorized(self):
        self.assertEqual(self._post("wagtail_not_a_real_token").status_code, 401)

    def test_inactive_user_unauthorized(self):
        token = token_for(make_user("ghost", superuser=True, active=False))
        self.assertEqual(self._post(token).status_code, 401)

    def test_revoked_token_unauthorized(self):
        token = token_for(make_user("revoked_user", superuser=True), revoked=True)
        self.assertEqual(self._post(token).status_code, 401)


@override_settings(VIDEOGEN_API_KEY="sk_test_dummy")
class StartVideoBehaviourTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.article = create_blog_article()

    def setUp(self):
        self.spawn = mock.patch.object(services, "spawn_worker").start()
        mock.patch.object(services, "get_client").start()
        self.addCleanup(mock.patch.stopall)
        self.token = token_for(make_user("boss", superuser=True))

    def _post(self, page_id=None):
        return self.client.post(
            start_url(page_id or self.article.id),
            content_type="application/json",
            **bearer(self.token),
        )

    def test_idempotent_second_request_returns_same_job(self):
        first = self._post()
        second = self._post()
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(ArticleVideo.objects.count(), 1)
        # Only one production run is ever launched -> never billed twice.
        self.spawn.assert_called_once()

    def test_non_blog_page_rejected(self):
        index = self.article.get_parent()  # BlogIndexPage, not a BlogPage
        self.assertEqual(self._post(index.id).status_code, 400)

    def test_unpublished_article_conflict(self):
        draft = create_blog_article(title="Draft", live=False)
        resp = self._post(draft.id)
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(ArticleVideo.objects.exists())

    def test_missing_page_404(self):
        self.assertEqual(self._post(999999).status_code, 404)

    @override_settings(VIDEOGEN_API_KEY=None)
    def test_unconfigured_returns_503(self):
        self.assertEqual(self._post().status_code, 503)
        self.assertFalse(ArticleVideo.objects.exists())


@override_settings(VIDEOGEN_API_KEY="sk_test_dummy")
class VideoStatusTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.article = create_blog_article()

    def setUp(self):
        mock.patch.object(services, "get_client").start()
        self.addCleanup(mock.patch.stopall)
        self.token = token_for(make_user("boss", superuser=True))

    def _get(self, job_id, token=None):
        return self.client.get(
            status_url(self.article.id, job_id), **bearer(token or self.token)
        )

    def test_processing_status(self):
        job = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.PROCESSING, progress_percentage=42
        )
        body = self._get(job.id).json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progressPercentage"], 42)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])
        self.assertEqual(body["videoJobId"], str(job.id))

    def test_ready_status_carries_download_url(self):
        job = ArticleVideo.objects.create(
            page=self.article,
            status=VideoStatus.READY,
            progress_percentage=100,
            download_url="https://signed.example/v.mp4",
            download_url_signed_at=timezone.now(),
        )
        body = self._get(job.id).json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["downloadUrl"], "https://signed.example/v.mp4")
        self.assertIsNone(body["error"])

    def test_failed_status_carries_error(self):
        job = ArticleVideo.objects.create(
            page=self.article,
            status=VideoStatus.FAILED,
            error="VideoGen rejected the script.",
        )
        body = self._get(job.id).json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "VideoGen rejected the script.")
        self.assertIsNone(body["downloadUrl"])

    def test_unknown_job_404(self):
        import uuid

        self.assertEqual(self._get(uuid.uuid4()).status_code, 404)

    def test_job_for_other_page_404(self):
        other = create_blog_article(title="Other")
        job = ArticleVideo.objects.create(page=other, status=VideoStatus.PROCESSING)
        # Looked up under self.article, but the job belongs to `other`.
        self.assertEqual(self._get(job.id).status_code, 404)

    def test_status_requires_publish_permission(self):
        job = ArticleVideo.objects.create(page=self.article, status=VideoStatus.PROCESSING)
        editor_token = token_for(make_user("ed", group="Editors"))
        self.assertEqual(self._get(job.id, token=editor_token).status_code, 403)

    def test_status_requires_auth(self):
        job = ArticleVideo.objects.create(page=self.article, status=VideoStatus.PROCESSING)
        resp = self.client.get(status_url(self.article.id, job.id))
        self.assertEqual(resp.status_code, 401)
