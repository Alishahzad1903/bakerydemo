from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.base import ContentFile
from django.test import TestCase
from wagtail.models import APIToken, GroupPagePermission, Page
from wagtail.rich_text import RichText

from bakerydemo.blog.models import BlogPage
from bakerydemo.breads.models import BreadPage
from bakerydemo.videos.constants import VideoStatus
from bakerydemo.videos.models import ArticleVideo

User = get_user_model()


class VideoApiTestBase(TestCase):
    def setUp(self):
        self.root = Page.get_first_root_node()
        self.article = self.root.add_child(
            instance=BlogPage(
                title="Tracking Wild Yeast",
                slug="tracking-wild-yeast",
                introduction="How we chase wild yeast.",
                body=[("paragraph_block", RichText("<p>First.</p><p>Second.</p>"))],
                live=True,
            )
        )
        # Superuser: may publish anything.
        self.superuser = User.objects.create_superuser("admin_t", "a@example.com", "pw")
        # Publisher: non-superuser granted publish via a group page permission.
        self.publisher = User.objects.create_user("pub_t", "p@example.com", "pw")
        publishers = Group.objects.create(name="Publishers")
        publish_perm = Permission.objects.get(
            content_type__app_label="wagtailcore", codename="publish_page"
        )
        GroupPagePermission.objects.create(
            group=publishers, page=self.root, permission=publish_perm
        )
        self.publisher.groups.add(publishers)
        # Editor: active, but has no publish permission.
        self.editor = User.objects.create_user("ed_t", "e@example.com", "pw")

    def token_for(self, user):
        _, plaintext = APIToken.create_token(user=user, name=f"tok-{user.pk}")
        return plaintext

    def auth(self, user):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token_for(user)}"}

    def video_url(self, page_id=None, job_id=None, download=False):
        page_id = self.article.id if page_id is None else page_id
        base = f"/api/v3-preview/pages/{page_id}/video/"
        if job_id is not None:
            base += f"{job_id}/"
            if download:
                base += "download/"
        return base


class CreateVideoPermissionTests(VideoApiTestBase):
    def _patch_runner(self):
        return mock.patch("bakerydemo.videos.runner.start_in_background")

    def test_superuser_can_start_and_gets_job_id(self):
        with self._patch_runner() as started, self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(self.video_url(), **self.auth(self.superuser))
        self.assertEqual(resp.status_code, 202)
        self.assertIn("videoJobId", resp.json())
        self.assertEqual(ArticleVideo.objects.count(), 1)
        started.assert_called_once()

    def test_publisher_without_superuser_can_start(self):
        with self._patch_runner(), self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(self.video_url(), **self.auth(self.publisher))
        self.assertEqual(resp.status_code, 202)

    def test_editor_without_publish_is_forbidden(self):
        with self._patch_runner() as started:
            resp = self.client.post(self.video_url(), **self.auth(self.editor))
        self.assertEqual(resp.status_code, 403)
        started.assert_not_called()
        self.assertEqual(ArticleVideo.objects.count(), 0)

    def test_unauthenticated_is_rejected(self):
        resp = self.client.post(self.video_url())
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_is_rejected(self):
        resp = self.client.post(
            self.video_url(), HTTP_AUTHORIZATION="Bearer not-a-real-token"
        )
        self.assertEqual(resp.status_code, 401)

    def test_non_blog_page_is_bad_request(self):
        bread = self.root.add_child(
            instance=BreadPage(title="Sourdough", slug="sourdough", live=True)
        )
        with self._patch_runner():
            resp = self.client.post(
                self.video_url(page_id=bread.id), **self.auth(self.superuser)
            )
        self.assertEqual(resp.status_code, 400)

    def test_unpublished_article_is_bad_request(self):
        draft = self.root.add_child(
            instance=BlogPage(
                title="Draft", slug="draft", introduction="x",
                body=[("paragraph_block", RichText("<p>x</p>"))], live=False,
            )
        )
        with self._patch_runner():
            resp = self.client.post(
                self.video_url(page_id=draft.id), **self.auth(self.superuser)
            )
        self.assertEqual(resp.status_code, 400)

    def test_missing_page_is_not_found(self):
        with self._patch_runner():
            resp = self.client.post(
                self.video_url(page_id=999999), **self.auth(self.superuser)
            )
        self.assertEqual(resp.status_code, 404)

    def test_repeated_request_is_idempotent_and_not_rebilled(self):
        with mock.patch("bakerydemo.videos.runner.start_in_background") as started, \
                self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(self.video_url(), **self.auth(self.superuser))
            second = self.client.post(self.video_url(), **self.auth(self.superuser))
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(ArticleVideo.objects.count(), 1)
        started.assert_called_once()  # only the first request starts production

    def test_failed_job_is_reset_and_restarted(self):
        av = ArticleVideo.objects.create(page=self.article, status=VideoStatus.FAILED,
                                         error_message="boom", progress_percentage=40)
        with mock.patch("bakerydemo.videos.runner.start_in_background") as started, \
                self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(self.video_url(), **self.auth(self.superuser))
        self.assertEqual(resp.status_code, 202)
        av.refresh_from_db()
        self.assertEqual(av.status, VideoStatus.PENDING)
        self.assertEqual(av.error_message, "")
        started.assert_called_once()


class VideoStatusTests(VideoApiTestBase):
    def test_processing_status_has_no_download_url(self):
        av = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.PROCESSING, progress_percentage=42.5
        )
        resp = self.client.get(
            self.video_url(job_id=av.job_id), **self.auth(self.superuser)
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "processing")
        self.assertEqual(data["progressPercentage"], 42.5)
        self.assertIsNone(data["downloadUrl"])
        self.assertIsNone(data["error"])

    def test_failed_status_carries_error(self):
        av = ArticleVideo.objects.create(
            page=self.article, status=VideoStatus.FAILED, error_message="out of credits"
        )
        resp = self.client.get(
            self.video_url(job_id=av.job_id), **self.auth(self.superuser)
        )
        data = resp.json()
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"], "out of credits")
        self.assertIsNone(data["downloadUrl"])

    def test_ready_status_has_absolute_download_url(self):
        av = ArticleVideo.objects.create(page=self.article, status=VideoStatus.READY,
                                         progress_percentage=100)
        av.video_file.save("v.mp4", ContentFile(b"MP4"), save=True)
        resp = self.client.get(
            self.video_url(job_id=av.job_id), **self.auth(self.superuser)
        )
        data = resp.json()
        self.assertEqual(data["status"], "ready")
        self.assertTrue(data["downloadUrl"].startswith("http"))
        self.assertIn(f"/video/{av.job_id}/download/", data["downloadUrl"])

    def test_unknown_job_is_not_found(self):
        resp = self.client.get(
            self.video_url(job_id="00000000-0000-0000-0000-000000000000"),
            **self.auth(self.superuser),
        )
        self.assertEqual(resp.status_code, 404)

    def test_status_requires_publish_permission(self):
        av = ArticleVideo.objects.create(page=self.article, status=VideoStatus.PROCESSING)
        resp = self.client.get(
            self.video_url(job_id=av.job_id), **self.auth(self.editor)
        )
        self.assertEqual(resp.status_code, 403)


class VideoDownloadTests(VideoApiTestBase):
    def test_download_streams_the_stored_mp4(self):
        av = ArticleVideo.objects.create(page=self.article, status=VideoStatus.READY,
                                         progress_percentage=100)
        av.video_file.save("v.mp4", ContentFile(b"MP4-CONTENT"), save=True)
        resp = self.client.get(
            self.video_url(job_id=av.job_id, download=True), **self.auth(self.superuser)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "video/mp4")
        self.assertEqual(b"".join(resp.streaming_content), b"MP4-CONTENT")

    def test_download_not_ready_is_not_found(self):
        av = ArticleVideo.objects.create(page=self.article, status=VideoStatus.PROCESSING)
        resp = self.client.get(
            self.video_url(job_id=av.job_id, download=True), **self.auth(self.superuser)
        )
        self.assertEqual(resp.status_code, 404)

    def test_download_requires_publish_permission(self):
        av = ArticleVideo.objects.create(page=self.article, status=VideoStatus.READY,
                                         progress_percentage=100)
        av.video_file.save("v.mp4", ContentFile(b"MP4"), save=True)
        resp = self.client.get(
            self.video_url(job_id=av.job_id, download=True), **self.auth(self.editor)
        )
        self.assertEqual(resp.status_code, 403)
