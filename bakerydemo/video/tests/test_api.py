from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from wagtail.models import APIToken

from bakerydemo.video.models import VideoJob

from .fakes import FakeVideoGenClient
from .helpers import create_blog_article

User = get_user_model()


@override_settings(VIDEOGEN_API_KEY="sk_test", VIDEOGEN_EXECUTE_INLINE=True)
class VideoAPITests(TestCase):
    def setUp(self):
        self.article = create_blog_article()
        self.url = f"/api/v3-preview/pages/{self.article.pk}/video/"

        self.superuser = User.objects.create_user(
            "boss", "boss@example.com", "pw", is_superuser=True, is_staff=True
        )
        _, self.super_token = APIToken.create_token(user=self.superuser, name="super")

        self.plain = User.objects.create_user(
            "plain", "plain@example.com", "pw", is_staff=True
        )
        _, self.plain_token = APIToken.create_token(user=self.plain, name="plain")

    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    # --- authentication / authorization ---
    def test_anonymous_is_rejected(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 401)

    def test_invalid_token_rejected(self):
        response = self.client.post(self.url, **self._auth("wagtail_nope"))
        self.assertEqual(response.status_code, 401)

    def test_revoked_token_rejected(self):
        token_obj, raw = APIToken.create_token(user=self.superuser, name="revoked")
        token_obj.revoke()
        response = self.client.post(self.url, **self._auth(raw))
        self.assertEqual(response.status_code, 401)

    def test_inactive_user_rejected(self):
        self.superuser.is_active = False
        self.superuser.save()
        response = self.client.post(self.url, **self._auth(self.super_token))
        self.assertEqual(response.status_code, 401)

    def test_caller_without_publish_permission_forbidden(self):
        response = self.client.post(self.url, **self._auth(self.plain_token))
        self.assertEqual(response.status_code, 403)

    def test_non_blog_page_not_found(self):
        from wagtail.models import Page

        root = Page.objects.filter(depth=1).first()
        url = f"/api/v3-preview/pages/{root.pk}/video/"
        response = self.client.post(url, **self._auth(self.super_token))
        self.assertEqual(response.status_code, 404)

    # --- happy path + idempotency ---
    def test_publisher_starts_video_and_polls_to_ready(self):
        fake = FakeVideoGenClient()
        with mock.patch(
            "bakerydemo.video.service.VideoGenClient.from_settings", return_value=fake
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url, **self._auth(self.super_token))

        self.assertEqual(response.status_code, 202)
        job_id = response.json()["videoJobId"]

        status_url = f"{self.url}{job_id}/"
        status = self.client.get(status_url, **self._auth(self.super_token))
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["progressPercentage"], 100)
        self.assertEqual(body["downloadUrl"], fake.download_url)
        self.assertIsNone(body["error"])

    def test_repeated_request_is_idempotent(self):
        fake = FakeVideoGenClient()
        with mock.patch(
            "bakerydemo.video.service.VideoGenClient.from_settings", return_value=fake
        ):
            with self.captureOnCommitCallbacks(execute=True):
                first = self.client.post(self.url, **self._auth(self.super_token))
            with self.captureOnCommitCallbacks(execute=True):
                second = self.client.post(self.url, **self._auth(self.super_token))

        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        # The billable workflow was started exactly once.
        self.assertEqual(len(fake.script_calls), 1)

    def test_status_requires_matching_page(self):
        fake = FakeVideoGenClient()
        with mock.patch(
            "bakerydemo.video.service.VideoGenClient.from_settings", return_value=fake
        ):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url, **self._auth(self.super_token))
        job_id = response.json()["videoJobId"]

        other = create_blog_article(title="Another")
        wrong_url = f"/api/v3-preview/pages/{other.pk}/video/{job_id}/"
        resp = self.client.get(wrong_url, **self._auth(self.super_token))
        self.assertEqual(resp.status_code, 404)

    @override_settings(VIDEOGEN_API_KEY=None)
    def test_unconfigured_returns_503(self):
        response = self.client.post(self.url, **self._auth(self.super_token))
        self.assertEqual(response.status_code, 503)
