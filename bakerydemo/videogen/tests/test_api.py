from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.base import ContentFile
from django.test import TestCase
from wagtail.models import APIToken, GroupPagePermission, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videogen import service
from bakerydemo.videogen.models import JobStatus, VideoJob

User = get_user_model()


def _perm(codename):
    return Permission.objects.get(
        content_type__app_label="wagtailcore", codename=codename
    )


class VideoApiTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        cls.index = BlogIndexPage(title="Blog", slug="blog-api")
        root.add_child(instance=cls.index)
        cls.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="twy-api",
            introduction="Yeasts grow as single cells. Ignored.",
            live=True,
        )
        cls.index.add_child(instance=cls.article)
        cls.draft = BlogPage(
            title="Draft Article", slug="draft-api", introduction="Hidden.", live=False
        )
        cls.index.add_child(instance=cls.draft)

        # Groups mirroring the demo: Moderators can publish, Editors cannot.
        mods = Group.objects.create(name="VideoModerators")
        GroupPagePermission.objects.create(group=mods, page=root, permission=_perm("publish_page"))
        GroupPagePermission.objects.create(group=mods, page=root, permission=_perm("add_page"))
        GroupPagePermission.objects.create(group=mods, page=root, permission=_perm("change_page"))
        editors = Group.objects.create(name="VideoEditors")
        GroupPagePermission.objects.create(group=editors, page=root, permission=_perm("add_page"))
        GroupPagePermission.objects.create(group=editors, page=root, permission=_perm("change_page"))

        cls.admin = User.objects.create(username="admin", is_superuser=True, is_active=True)
        cls.moderator = User.objects.create(username="moderator", is_active=True)
        cls.moderator.groups.add(mods)
        cls.editor = User.objects.create(username="editor", is_active=True)
        cls.editor.groups.add(editors)
        cls.inactive = User.objects.create(username="inactive", is_superuser=True, is_active=False)

        cls.tokens = {}
        for user in (cls.admin, cls.moderator, cls.editor, cls.inactive):
            _, plaintext = APIToken.create_token(user=user, name=f"{user.username} token")
            cls.tokens[user.username] = plaintext
        _, revoked = APIToken.create_token(user=cls.admin, name="revoked")
        APIToken.objects.get(prefix=revoked[:12], name="revoked").revoke()
        cls.revoked_token = revoked

    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def create_url(self, page=None):
        page = page or self.article
        return f"/api/v3-preview/pages/{page.id}/video/"

    def detail_url(self, job, page=None):
        page = page or self.article
        return f"/api/v3-preview/pages/{page.id}/video/{job.pk}/"

    def download_url(self, job, page=None):
        page = page or self.article
        return f"/api/v3-preview/pages/{page.id}/video/{job.pk}/download/"


class CreateAuthMatrixTests(VideoApiTestBase):
    def setUp(self):
        self._spawn = mock.patch.object(service, "_spawn").start()
        self.addCleanup(mock.patch.stopall)

    def test_admin_can_start(self):
        resp = self.client.post(self.create_url(), **self._auth(self.tokens["admin"]))
        self.assertEqual(resp.status_code, 202)
        self.assertIn("videoJobId", resp.json())

    def test_moderator_can_start(self):
        resp = self.client.post(self.create_url(), **self._auth(self.tokens["moderator"]))
        self.assertEqual(resp.status_code, 202)

    def test_editor_forbidden(self):
        resp = self.client.post(self.create_url(), **self._auth(self.tokens["editor"]))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(VideoJob.objects.count(), 0)

    def test_no_token_unauthorized(self):
        resp = self.client.post(self.create_url())
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_unauthorized(self):
        resp = self.client.post(self.create_url(), **self._auth("wagtail_notarealtoken"))
        self.assertEqual(resp.status_code, 401)

    def test_revoked_token_unauthorized(self):
        resp = self.client.post(self.create_url(), **self._auth(self.revoked_token))
        self.assertEqual(resp.status_code, 401)

    def test_inactive_user_unauthorized(self):
        resp = self.client.post(self.create_url(), **self._auth(self.tokens["inactive"]))
        self.assertEqual(resp.status_code, 401)

    def test_non_blog_page_not_found(self):
        resp = self.client.post(
            self.create_url(self.index), **self._auth(self.tokens["admin"])
        )
        self.assertEqual(resp.status_code, 404)

    def test_draft_article_conflict(self):
        resp = self.client.post(
            self.create_url(self.draft), **self._auth(self.tokens["admin"])
        )
        self.assertEqual(resp.status_code, 409)


class CreateIdempotencyTests(VideoApiTestBase):
    def setUp(self):
        mock.patch.object(service, "_spawn").start()
        self.addCleanup(mock.patch.stopall)

    def test_second_request_returns_same_job(self):
        r1 = self.client.post(self.create_url(), **self._auth(self.tokens["admin"]))
        r2 = self.client.post(self.create_url(), **self._auth(self.tokens["admin"]))
        self.assertEqual(r1.status_code, 202)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json()["videoJobId"], r2.json()["videoJobId"])
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)


class DetailTests(VideoApiTestBase):
    def _make_job(self, **kwargs):
        return VideoJob.objects.create(page=self.article, **kwargs)

    def test_processing_representation(self):
        job = self._make_job(status=JobStatus.PROCESSING, progress_percentage=40)
        resp = self.client.get(self.detail_url(job), **self._auth(self.tokens["admin"]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["videoJobId"], str(job.pk))
        self.assertEqual(data["status"], "processing")
        self.assertEqual(data["progressPercentage"], 40)
        self.assertIsNone(data["downloadUrl"])
        self.assertIsNone(data["error"])

    def test_ready_representation_has_download_url(self):
        job = self._make_job(status=JobStatus.SUCCEEDED, progress_percentage=100)
        job.video_file.save("v.mp4", ContentFile(b"MP4"), save=True)
        resp = self.client.get(self.detail_url(job), **self._auth(self.tokens["admin"]))
        data = resp.json()
        self.assertEqual(data["status"], "ready")
        self.assertTrue(data["downloadUrl"].endswith(f"/video/{job.pk}/download/"))

    def test_failed_representation_has_error(self):
        job = self._make_job(status=JobStatus.FAILED, error="it broke")
        resp = self.client.get(self.detail_url(job), **self._auth(self.tokens["admin"]))
        data = resp.json()
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], "it broke")
        self.assertIsNone(data["downloadUrl"])

    def test_detail_requires_publish_permission(self):
        job = self._make_job(status=JobStatus.PROCESSING)
        resp = self.client.get(self.detail_url(job), **self._auth(self.tokens["editor"]))
        self.assertEqual(resp.status_code, 403)

    def test_unknown_job_not_found(self):
        resp = self.client.get(
            f"/api/v3-preview/pages/{self.article.id}/video/"
            "00000000-0000-0000-0000-000000000000/",
            **self._auth(self.tokens["admin"]),
        )
        self.assertEqual(resp.status_code, 404)

    def test_job_scoped_to_its_page(self):
        job = self._make_job(status=JobStatus.SUCCEEDED)
        # Same job id, but requested under a different (wrong) page id.
        other = BlogPage(title="Other", slug="other-api", introduction="x.", live=True)
        self.index.add_child(instance=other)
        resp = self.client.get(self.detail_url(job, page=other), **self._auth(self.tokens["admin"]))
        self.assertEqual(resp.status_code, 404)


class DownloadTests(VideoApiTestBase):
    def test_download_returns_mp4(self):
        job = VideoJob.objects.create(page=self.article, status=JobStatus.SUCCEEDED)
        job.video_file.save("v.mp4", ContentFile(b"REALMP4BYTES"), save=True)
        resp = self.client.get(self.download_url(job), **self._auth(self.tokens["admin"]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "video/mp4")
        self.assertEqual(b"".join(resp.streaming_content), b"REALMP4BYTES")

    def test_download_not_ready_is_404(self):
        job = VideoJob.objects.create(page=self.article, status=JobStatus.PROCESSING)
        resp = self.client.get(self.download_url(job), **self._auth(self.tokens["admin"]))
        self.assertEqual(resp.status_code, 404)

    def test_download_requires_publish_permission(self):
        job = VideoJob.objects.create(page=self.article, status=JobStatus.SUCCEEDED)
        job.video_file.save("v.mp4", ContentFile(b"X"), save=True)
        resp = self.client.get(self.download_url(job), **self._auth(self.tokens["editor"]))
        self.assertEqual(resp.status_code, 403)
