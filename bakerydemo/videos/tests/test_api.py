import json
import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings

from wagtail.models import APIToken, GroupPagePermission, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos.models import VideoJob, VideoJobStatus

from .fakes import FakeVideoGenClient

_MEDIA = tempfile.mkdtemp(prefix="videogen-api-test-")

User = get_user_model()


@override_settings(MEDIA_ROOT=_MEDIA, VIDEOGEN_RUN_SYNC=True)
class VideoApiTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        root = Page.objects.get(depth=1)
        self.index = root.add_child(
            instance=BlogIndexPage(title="Blog", slug="blog-idx")
        )
        self.article = self.index.add_child(
            instance=BlogPage(
                title="Tracking Wild Yeast",
                slug="wild-yeast",
                introduction="Yeasts can be contrasted with molds. Tail ignored.",
                live=True,
            )
        )
        self.draft = self.index.add_child(
            instance=BlogPage(
                title="Unpublished", slug="draft", introduction="Secret.", live=False
            )
        )

        # Permission setup mirrors the demo: superuser + a publisher group + an
        # edit-only group.
        publishers = Group.objects.create(name="Publishers")
        GroupPagePermission.objects.create(
            group=publishers, page=self.index, permission_type="publish"
        )
        GroupPagePermission.objects.create(
            group=publishers, page=self.index, permission_type="change"
        )
        editors = Group.objects.create(name="EditorsOnly")
        GroupPagePermission.objects.create(
            group=editors, page=self.index, permission_type="change"
        )

        self.superuser = User.objects.create_user(
            "su", password="x", is_superuser=True, is_staff=True
        )
        self.publisher = User.objects.create_user("pub", password="x")
        self.publisher.groups.add(publishers)
        self.editor = User.objects.create_user("ed", password="x")
        self.editor.groups.add(editors)
        self.inactive = User.objects.create_user("ina", password="x")
        self.inactive.groups.add(publishers)
        self.inactive.is_active = False
        self.inactive.save()

        self.tokens = {}
        for name, user in [
            ("su", self.superuser),
            ("pub", self.publisher),
            ("ed", self.editor),
            ("ina", self.inactive),
        ]:
            _, plaintext = APIToken.create_token(user=user, name=name)
            self.tokens[name] = plaintext
        revoked_tok, revoked_plain = APIToken.create_token(
            user=self.superuser, name="revoked"
        )
        revoked_tok.revoke()
        self.tokens["revoked"] = revoked_plain

    # -- helpers ------------------------------------------------------------

    def _auth(self, who):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.tokens[who]}"}

    def _post_video(self, page, who=None):
        headers = self._auth(who) if who else {}
        return self.client.post(
            f"/api/v3-preview/pages/{page.id}/video/",
            data="",
            content_type="application/json",
            **headers,
        )

    def _fake(self):
        return mock.patch(
            "bakerydemo.videos.services.get_client",
            return_value=FakeVideoGenClient(),
        )

    # -- authentication / authorization ------------------------------------

    def test_post_requires_authentication(self):
        self.assertEqual(self._post_video(self.article).status_code, 401)

    def test_revoked_token_is_rejected(self):
        self.assertEqual(
            self._post_video(self.article, "revoked").status_code, 401
        )

    def test_inactive_user_token_is_rejected(self):
        self.assertEqual(self._post_video(self.article, "ina").status_code, 401)

    def test_editor_without_publish_is_forbidden(self):
        with self._fake():
            resp = self._post_video(self.article, "ed")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(VideoJob.objects.count(), 0)

    def test_superuser_can_start_and_publisher_is_idempotent(self):
        with self._fake() as patched:
            resp = self._post_video(self.article, "su")
            self.assertEqual(resp.status_code, 202)
            job_id = resp.json()["videoJobId"]
            self.assertTrue(job_id)

            # A second, permitted caller gets the SAME job (no new production).
            resp2 = self._post_video(self.article, "pub")
            self.assertEqual(resp2.status_code, 200)
            self.assertEqual(resp2.json()["videoJobId"], job_id)

        self.assertEqual(VideoJob.objects.count(), 1)
        # create_script_to_video was invoked exactly once across both calls.
        self.assertEqual(patched.return_value.count("create_script_to_video"), 1)

    # -- validation ---------------------------------------------------------

    def test_non_blog_page_is_rejected(self):
        with self._fake():
            resp = self._post_video(self.index, "su")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(VideoJob.objects.count(), 0)

    def test_unpublished_article_is_rejected(self):
        with self._fake():
            resp = self._post_video(self.draft, "su")
        self.assertEqual(resp.status_code, 422)

    # -- status + download --------------------------------------------------

    def test_status_reports_ready_and_download_url(self):
        with self._fake():
            job_id = self._post_video(self.article, "su").json()["videoJobId"]

        resp = self.client.get(
            f"/api/v3-preview/pages/{self.article.id}/video/{job_id}/",
            **self._auth("su"),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["progressPercentage"], 100)
        self.assertIsNone(body["error"])
        self.assertTrue(body["downloadUrl"])
        self.assertIn(f"/video/{job_id}/download/", body["downloadUrl"])

    def test_status_forbidden_for_editor(self):
        with self._fake():
            job_id = self._post_video(self.article, "su").json()["videoJobId"]
        resp = self.client.get(
            f"/api/v3-preview/pages/{self.article.id}/video/{job_id}/",
            **self._auth("ed"),
        )
        self.assertEqual(resp.status_code, 403)

    def test_status_unknown_job_is_404(self):
        import uuid

        resp = self.client.get(
            f"/api/v3-preview/pages/{self.article.id}/video/{uuid.uuid4()}/",
            **self._auth("su"),
        )
        self.assertEqual(resp.status_code, 404)

    def test_download_streams_mp4(self):
        with self._fake():
            job_id = self._post_video(self.article, "su").json()["videoJobId"]
        resp = self.client.get(
            f"/api/v3-preview/pages/{self.article.id}/video/{job_id}/download/",
            **self._auth("su"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "video/mp4")
        content = b"".join(resp.streaming_content)
        self.assertTrue(content.startswith(b"\x00\x00\x00\x18ftyp"))

    def test_download_requires_publish_permission(self):
        with self._fake():
            job_id = self._post_video(self.article, "su").json()["videoJobId"]
        resp = self.client.get(
            f"/api/v3-preview/pages/{self.article.id}/video/{job_id}/download/",
            **self._auth("ed"),
        )
        self.assertEqual(resp.status_code, 403)

    def test_status_reports_error_when_failed(self):
        job = VideoJob.objects.create(
            page=self.article,
            status=VideoJobStatus.FAILED,
            progress_percentage=1,
            error="Video generation failed: stock footage unavailable",
        )
        resp = self.client.get(
            f"/api/v3-preview/pages/{self.article.id}/video/{job.id}/",
            **self._auth("su"),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "failed")
        self.assertIn("stock footage unavailable", body["error"])
        self.assertIsNone(body["downloadUrl"])

    def test_pending_job_download_is_404(self):
        job = VideoJob.objects.create(
            page=self.article, status=VideoJobStatus.PROCESSING, script="s"
        )
        resp = self.client.get(
            f"/api/v3-preview/pages/{self.article.id}/video/{job.id}/download/",
            **self._auth("su"),
        )
        self.assertEqual(resp.status_code, 404)
