from unittest import mock

from django.test import TestCase
from django.urls import reverse

from bakerydemo.videogen import services
from bakerydemo.videogen.models import VideoJob, VideoJobStatus

from .factories import (
    build_blog_tree,
    make_groups,
    make_user_with_token,
)


class VideoApiTestCase(TestCase):
    def setUp(self):
        self.tree = build_blog_tree()
        self.article = self.tree["article"]
        self.draft = self.tree["draft"]
        moderators, editors = make_groups(self.tree["root"])

        _, self.admin_token = make_user_with_token("t_admin", is_superuser=True)
        _, self.arabic_token = make_user_with_token("t_arabic", is_superuser=True)
        _, self.moderator_token = make_user_with_token("t_moderator", group=moderators)
        _, self.editor_token = make_user_with_token("t_editor", group=editors)
        _, self.inactive_token = make_user_with_token(
            "t_inactive", is_superuser=True, is_active=False
        )
        _, self.revoked_token = make_user_with_token(
            "t_revoked", is_superuser=True, revoked=True
        )

        self.create_url = reverse(
            "wagtailapi_v3:pages_video_create", kwargs={"page_id": self.article.pk}
        )

    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def post_create(self, token=None, page_id=None):
        url = (
            self.create_url
            if page_id is None
            else reverse(
                "wagtailapi_v3:pages_video_create", kwargs={"page_id": page_id}
            )
        )
        headers = self._auth(token) if token else {}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, **headers)


class AuthMatrixTests(VideoApiTestCase):
    def setUp(self):
        super().setUp()
        # Keep tests hermetic: never actually spawn a worker/thread.
        patcher = mock.patch.object(services, "enqueue")
        self.enqueue = patcher.start()
        self.addCleanup(patcher.stop)

    def test_admin_superuser_allowed(self):
        resp = self.post_create(self.admin_token)
        self.assertEqual(resp.status_code, 202)
        self.assertIn("videoJobId", resp.json())

    def test_other_superuser_allowed(self):
        self.assertEqual(self.post_create(self.arabic_token).status_code, 202)

    def test_moderator_with_publish_permission_allowed(self):
        self.assertEqual(self.post_create(self.moderator_token).status_code, 202)

    def test_editor_without_publish_permission_forbidden(self):
        self.assertEqual(self.post_create(self.editor_token).status_code, 403)

    def test_inactive_user_unauthorized(self):
        self.assertEqual(self.post_create(self.inactive_token).status_code, 401)

    def test_revoked_token_unauthorized(self):
        self.assertEqual(self.post_create(self.revoked_token).status_code, 401)

    def test_missing_token_unauthorized(self):
        self.assertEqual(self.post_create(None).status_code, 401)

    def test_garbage_token_unauthorized(self):
        self.assertEqual(self.post_create("not-a-real-token").status_code, 401)


class IdempotencyTests(VideoApiTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(services, "enqueue")
        self.enqueue = patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_posts_return_same_job_and_enqueue_once(self):
        first = self.post_create(self.admin_token)
        second = self.post_create(self.admin_token)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(VideoJob.objects.count(), 1)
        self.enqueue.assert_called_once()


class ResolutionAndValidationTests(VideoApiTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(services, "enqueue")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_blog_page_not_found(self):
        resp = self.post_create(self.admin_token, page_id=self.tree["index"].pk)
        self.assertEqual(resp.status_code, 404)

    def test_missing_page_not_found(self):
        resp = self.post_create(self.admin_token, page_id=999999)
        self.assertEqual(resp.status_code, 404)

    def test_draft_blog_page_unprocessable(self):
        resp = self.post_create(self.admin_token, page_id=self.draft.pk)
        self.assertEqual(resp.status_code, 422)


class StatusEndpointTests(VideoApiTestCase):
    def _detail_url(self, job, page=None):
        return reverse(
            "wagtailapi_v3:pages_video_detail",
            kwargs={
                "page_id": (page or self.article).pk,
                "video_job_id": str(job.uuid),
            },
        )

    def test_status_reports_processing(self):
        job = VideoJob.objects.create(
            page=self.article,
            narration_script="x",
            status=VideoJobStatus.PROCESSING,
            progress_percentage=42,
        )
        resp = self.client.get(self._detail_url(job), **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progressPercentage"], 42)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])

    def test_status_reports_ready_with_download_url(self):
        from django.core.files.base import ContentFile

        job = VideoJob.objects.create(
            page=self.article,
            narration_script="x",
            status=VideoJobStatus.READY,
            progress_percentage=100,
        )
        job.video_file.save("v.mp4", ContentFile(b"MP4"), save=True)
        self.addCleanup(lambda: job.video_file.delete(save=False))
        resp = self.client.get(self._detail_url(job), **self._auth(self.admin_token))
        body = resp.json()
        self.assertEqual(body["status"], "ready")
        self.assertTrue(body["downloadUrl"])
        self.assertIn(".mp4", body["downloadUrl"])

    def test_status_reports_failed_with_error(self):
        job = VideoJob.objects.create(
            page=self.article,
            narration_script="x",
            status=VideoJobStatus.FAILED,
            error="Export ended with status 'failed'",
        )
        resp = self.client.get(self._detail_url(job), **self._auth(self.admin_token))
        body = resp.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "Export ended with status 'failed'")

    def test_status_requires_publish_permission(self):
        job = VideoJob.objects.create(page=self.article, narration_script="x")
        resp = self.client.get(self._detail_url(job), **self._auth(self.editor_token))
        self.assertEqual(resp.status_code, 403)

    def test_status_unknown_job_not_found(self):
        import uuid

        url = reverse(
            "wagtailapi_v3:pages_video_detail",
            kwargs={"page_id": self.article.pk, "video_job_id": str(uuid.uuid4())},
        )
        resp = self.client.get(url, **self._auth(self.admin_token))
        self.assertEqual(resp.status_code, 404)
