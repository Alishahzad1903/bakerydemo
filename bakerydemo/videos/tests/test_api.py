from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos import service
from bakerydemo.videos.models import JobStatus, VideoJob

User = get_user_model()


def _token_for(user, name):
    _instance, plaintext = APIToken.create_token(user=user, name=name)
    return plaintext


class VideoEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        cls.index = root.add_child(
            instance=BlogIndexPage(title="Blog", slug="blog", introduction="Blog")
        )
        cls.article = cls.index.add_child(
            instance=BlogPage(
                title="Tracking Wild Yeast",
                slug="twy",
                introduction="Yeasts grow as single cells. A second sentence.",
            )
        )
        cls.article.save_revision().publish()
        cls.article.refresh_from_db()

        cls.draft = cls.index.add_child(
            instance=BlogPage(
                title="Draft Article", slug="draft", introduction="Not live yet."
            )
        )
        cls.draft.unpublish()  # ensure not live

        # Users mirroring the seeded roles.
        cls.admin = User.objects.create_superuser("t_admin", "a@e.com", "pw")
        cls.editor = User.objects.create_user("t_editor", "e@e.com", "pw")
        cls.editor.groups.add(Group.objects.get(name="Editors"))
        cls.moderator = User.objects.create_user("t_mod", "m@e.com", "pw")
        cls.moderator.groups.add(Group.objects.get(name="Moderators"))

        cls.admin_token = _token_for(cls.admin, "admin")
        cls.editor_token = _token_for(cls.editor, "editor")
        cls.moderator_token = _token_for(cls.moderator, "moderator")

    def setUp(self):
        # Never start a real (billable) production from a test.
        patcher = mock.patch.object(service, "_spawn_pipeline")
        self.spawn = patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, page_id, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(f"/api/v3-preview/pages/{page_id}/video/", **headers)

    def _get(self, page_id, job_id, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(
            f"/api/v3-preview/pages/{page_id}/video/{job_id}/", **headers
        )

    # -- POST: permissions -------------------------------------------------
    def test_admin_can_start(self):
        resp = self._post(self.article.pk, self.admin_token)
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertIn("videoJobId", data)
        self.assertEqual(data["status"], JobStatus.PENDING)
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        self.spawn.assert_called_once()

    def test_moderator_can_start(self):
        resp = self._post(self.article.pk, self.moderator_token)
        self.assertEqual(resp.status_code, 202)

    def test_editor_is_forbidden(self):
        resp = self._post(self.article.pk, self.editor_token)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(VideoJob.objects.filter(page=self.article).exists())

    def test_anonymous_is_unauthorized(self):
        resp = self._post(self.article.pk)
        self.assertEqual(resp.status_code, 401)

    def test_revoked_token_is_unauthorized(self):
        instance, plaintext = APIToken.create_token(user=self.admin, name="revoked")
        instance.revoked_at = timezone.now()
        instance.save()
        resp = self._post(self.article.pk, plaintext)
        self.assertEqual(resp.status_code, 401)

    # -- POST: validation --------------------------------------------------
    def test_non_blog_page_is_rejected(self):
        resp = self._post(self.index.pk, self.admin_token)  # BlogIndexPage
        self.assertEqual(resp.status_code, 422)

    def test_draft_article_is_rejected(self):
        resp = self._post(self.draft.pk, self.admin_token)
        self.assertEqual(resp.status_code, 409)

    def test_unknown_page_is_not_found(self):
        resp = self._post(999999, self.admin_token)
        self.assertEqual(resp.status_code, 404)

    # -- POST: idempotency -------------------------------------------------
    def test_second_post_returns_same_job(self):
        first = self._post(self.article.pk, self.admin_token).json()
        second = self._post(self.article.pk, self.admin_token).json()
        self.assertEqual(first["videoJobId"], second["videoJobId"])
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        self.spawn.assert_called_once()  # only the first request starts production

    # -- GET ---------------------------------------------------------------
    def _make_job(self, **kw):
        defaults = {"page": self.article, "status": JobStatus.PROCESSING}
        defaults.update(kw)
        return VideoJob.objects.create(**defaults)

    def test_get_reports_processing(self):
        job = self._make_job(progress_percentage=42.0)
        resp = self._get(self.article.pk, job.id, self.admin_token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["videoJobId"], str(job.id))
        self.assertEqual(data["status"], JobStatus.PROCESSING)
        self.assertEqual(data["progressPercentage"], 42.0)
        self.assertIsNone(data["downloadUrl"])
        self.assertIsNone(data["error"])

    def test_get_reports_ready_with_download_url(self):
        job = self._make_job(
            status=JobStatus.SUCCEEDED,
            progress_percentage=100.0,
            download_url="https://cdn/v.mp4",
            download_url_expires_at=9_999_999_999,
        )
        resp = self._get(self.article.pk, job.id, self.admin_token)
        data = resp.json()
        self.assertEqual(data["status"], JobStatus.SUCCEEDED)
        self.assertEqual(data["downloadUrl"], "https://cdn/v.mp4")

    def test_get_reports_failure_with_error(self):
        job = self._make_job(status=JobStatus.FAILED, error_message="render failed")
        resp = self._get(self.article.pk, job.id, self.admin_token)
        data = resp.json()
        self.assertEqual(data["status"], JobStatus.FAILED)
        self.assertEqual(data["error"], "render failed")

    def test_get_editor_is_forbidden(self):
        job = self._make_job()
        resp = self._get(self.article.pk, job.id, self.editor_token)
        self.assertEqual(resp.status_code, 403)

    def test_get_unknown_job_is_not_found(self):
        resp = self._get(
            self.article.pk, "00000000-0000-0000-0000-000000000000", self.admin_token
        )
        self.assertEqual(resp.status_code, 404)

    def test_get_malformed_job_id_is_not_found(self):
        resp = self._get(self.article.pk, "not-a-uuid", self.admin_token)
        self.assertEqual(resp.status_code, 404)
