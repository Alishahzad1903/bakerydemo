"""HTTP API tests: auth, the publish-permission gate, idempotency, and states.

The background pipeline is mocked out (``run_in_background``), so no VideoGen
calls happen here — these tests are about the endpoint contract and access
control, exercised with real bearer tokens resolved exactly like the v3 API does.
"""

from __future__ import annotations

import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from wagtail.models import APIToken, GroupPagePermission, Page

from bakerydemo.video.models import ArticleVideo, VideoStatus

from .utils import create_blog_article

User = get_user_model()


def _token_for(user) -> str:
    _, plaintext = APIToken.create_token(user=user, name=f"{user.username} token")
    return plaintext


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


class ApiTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.objects.get(depth=1)

        # --- users -------------------------------------------------------
        cls.admin = User.objects.create_user(
            username="admin_t", password="x", is_superuser=True, is_active=True
        )
        cls.publisher = User.objects.create_user(
            username="publisher_t", password="x", is_active=True
        )
        cls.editor = User.objects.create_user(
            username="editor_t", password="x", is_active=True
        )
        cls.inactive = User.objects.create_user(
            username="inactive_t", password="x", is_superuser=True, is_active=False
        )

        publishers = Group.objects.create(name="Publishers_t")
        editors = Group.objects.create(name="Editors_t")
        cls.publisher.groups.add(publishers)
        cls.editor.groups.add(editors)

        def perm(codename):
            return Permission.objects.get(
                content_type__app_label="wagtailcore", codename=codename
            )

        # Publishers may publish; editors may only add/change (no publish).
        GroupPagePermission.objects.create(
            group=publishers, page=root, permission=perm("publish_page")
        )
        GroupPagePermission.objects.create(
            group=editors, page=root, permission=perm("add_page")
        )
        GroupPagePermission.objects.create(
            group=editors, page=root, permission=perm("change_page")
        )

        # --- tokens ------------------------------------------------------
        cls.admin_token = _token_for(cls.admin)
        cls.publisher_token = _token_for(cls.publisher)
        cls.editor_token = _token_for(cls.editor)
        cls.inactive_token = _token_for(cls.inactive)
        revoked, cls.revoked_token = APIToken.create_token(
            user=cls.admin, name="revoked"
        )
        revoked.revoke()

        # --- content -----------------------------------------------------
        cls.article = create_blog_article()
        cls.draft_article = create_blog_article(title="Draft One", live=False)

    def create_url(self, page_id=None):
        return reverse(
            "wagtailapi_v3:video_create",
            kwargs={"page_id": page_id or self.article.id},
        )

    def detail_url(self, job_id, page_id=None):
        return reverse(
            "wagtailapi_v3:video_detail",
            kwargs={"page_id": page_id or self.article.id, "job_id": str(job_id)},
        )

    def download_url(self, job_id, page_id=None):
        return reverse(
            "wagtailapi_v3:video_download",
            kwargs={"page_id": page_id or self.article.id, "job_id": str(job_id)},
        )


class AuthenticationTests(ApiTestBase):
    def test_no_token_is_401(self):
        self.assertEqual(self.client.post(self.create_url()).status_code, 401)

    def test_invalid_token_is_401(self):
        resp = self.client.post(self.create_url(), headers=_bearer("wagtail_nope"))
        self.assertEqual(resp.status_code, 401)

    def test_revoked_token_is_401(self):
        resp = self.client.post(self.create_url(), headers=_bearer(self.revoked_token))
        self.assertEqual(resp.status_code, 401)

    def test_inactive_user_token_is_401(self):
        resp = self.client.post(self.create_url(), headers=_bearer(self.inactive_token))
        self.assertEqual(resp.status_code, 401)


class PermissionTests(ApiTestBase):
    @patch("bakerydemo.video.api.run_in_background")
    def test_publisher_is_admitted(self, _mock):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                self.create_url(), headers=_bearer(self.publisher_token)
            )
        self.assertEqual(resp.status_code, 202)

    def test_editor_without_publish_is_403(self):
        resp = self.client.post(self.create_url(), headers=_bearer(self.editor_token))
        self.assertEqual(resp.status_code, 403)


class CreateTests(ApiTestBase):
    @patch("bakerydemo.video.api.run_in_background")
    def test_create_starts_job_once_and_is_idempotent(self, mock_run):
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(
                self.create_url(), headers=_bearer(self.admin_token)
            )
        self.assertEqual(first.status_code, 202)
        body = first.json()
        self.assertIn("videoJobId", body)
        self.assertEqual(body["status"], VideoStatus.PENDING)
        self.assertEqual(body["progressPercentage"], 0)
        self.assertIsNone(body["downloadUrl"])
        self.assertEqual(ArticleVideo.objects.filter(page=self.article).count(), 1)
        self.assertEqual(mock_run.call_count, 1)

        # Asking again returns the SAME job and does NOT start a second one.
        with self.captureOnCommitCallbacks(execute=True):
            second = self.client.post(
                self.create_url(), headers=_bearer(self.admin_token)
            )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["videoJobId"], body["videoJobId"])
        self.assertEqual(ArticleVideo.objects.filter(page=self.article).count(), 1)
        self.assertEqual(mock_run.call_count, 1)  # still only once

    def test_create_builds_narration_from_article(self):
        with (
            self.captureOnCommitCallbacks(execute=False),
            patch("bakerydemo.video.api.run_in_background"),
        ):
            self.client.post(self.create_url(), headers=_bearer(self.admin_token))
        job = ArticleVideo.objects.get(page=self.article)
        self.assertTrue(job.script.startswith(self.article.title))
        self.assertLessEqual(len(job.script.split()), 30)

    def test_create_on_non_blog_page_is_404(self):
        index_id = self.article.get_parent().id
        resp = self.client.post(
            self.create_url(page_id=index_id), headers=_bearer(self.admin_token)
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_on_draft_article_is_404(self):
        resp = self.client.post(
            self.create_url(page_id=self.draft_article.id),
            headers=_bearer(self.admin_token),
        )
        self.assertEqual(resp.status_code, 404)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DetailAndDownloadTests(ApiTestBase):
    def _ready_job(self):
        job = ArticleVideo.objects.create(
            page=self.article,
            status=VideoStatus.READY,
            progress_percentage=100,
            script="Title. Sentence",
        )
        job.video_file.save("video.mp4", ContentFile(b"MP4DATA"), save=True)
        return job

    def test_detail_reports_processing_state(self):
        job = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.PROCESSING, progress_percentage=40
        )
        resp = self.client.get(
            self.detail_url(job.job_id), headers=_bearer(self.admin_token)
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progressPercentage"], 40)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])

    def test_detail_reports_failure(self):
        job = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.FAILED, error="it failed"
        )
        body = self.client.get(
            self.detail_url(job.job_id), headers=_bearer(self.admin_token)
        ).json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "it failed")
        self.assertIsNone(body["downloadUrl"])

    def test_ready_job_exposes_download_url(self):
        job = self._ready_job()
        body = self.client.get(
            self.detail_url(job.job_id), headers=_bearer(self.admin_token)
        ).json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["progressPercentage"], 100)
        self.assertIsNotNone(body["downloadUrl"])
        self.assertTrue(body["downloadUrl"].endswith("/download/"))

    def test_download_streams_the_mp4(self):
        job = self._ready_job()
        resp = self.client.get(
            self.download_url(job.job_id), headers=_bearer(self.admin_token)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "video/mp4")
        self.assertEqual(b"".join(resp.streaming_content), b"MP4DATA")

    def test_download_before_ready_is_404(self):
        job = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.PROCESSING
        )
        resp = self.client.get(
            self.download_url(job.job_id), headers=_bearer(self.admin_token)
        )
        self.assertEqual(resp.status_code, 404)

    def test_detail_unknown_job_is_404(self):
        resp = self.client.get(
            self.detail_url("00000000-0000-0000-0000-000000000000"),
            headers=_bearer(self.admin_token),
        )
        self.assertEqual(resp.status_code, 404)

    def test_detail_requires_auth(self):
        job = ArticleVideo.objects.create(page=self.article)
        self.assertEqual(self.client.get(self.detail_url(job.job_id)).status_code, 401)

    def test_detail_forbidden_for_non_publisher(self):
        job = ArticleVideo.objects.create(page=self.article)
        resp = self.client.get(
            self.detail_url(job.job_id), headers=_bearer(self.editor_token)
        )
        self.assertEqual(resp.status_code, 403)
