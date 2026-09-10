"""
Tests for the article-video integration.

Every test mocks the VideoGen client, so the suite never contacts the provider
and never spends money. The one real end-to-end run is performed manually
against a seeded article (see the verification guide), not here.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from videogen.errors import VideoGenError as ProviderVideoGenError
from wagtail.models import APIToken, GroupPagePermission, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

from .exceptions import VideoGenConfigurationError, VideoGenServiceError
from .models import ArticleVideo
from .narration import build_script, first_sentence
from .service import produce_and_store

User = get_user_model()

BASE = "/api/v3-preview"


def _mock_client():
    """A VideoGen client mock whose happy path yields a stored MP4."""
    client = mock.MagicMock()
    client.workflows.script_to_video.return_value = {
        "workflow_run_id": "vg_work_test",
        "project_id": "vg_proj_test",
    }
    client.projects.export_project.return_value = {"export_id": "vg_exp_test"}
    client.download_file.return_value = b"FAKE-MP4-BYTES"
    return client


class NarrationTests(TestCase):
    def test_first_sentence(self):
        self.assertEqual(
            first_sentence("Hello world. Second sentence here."), "Hello world."
        )
        self.assertEqual(first_sentence("No punctuation at all"), "No punctuation at all")
        self.assertEqual(first_sentence(""), "")

    def test_build_script_title_and_first_sentence_only(self):
        script = build_script(
            "Tracking Wild Yeast",
            "Yeasts grow as single cells. This second sentence must be ignored.",
        )
        self.assertEqual(
            script, "Tracking Wild Yeast. Yeasts grow as single cells."
        )

    def test_build_script_word_ceiling(self):
        intro = " ".join(f"word{i}" for i in range(50)) + "."
        script = build_script("A Title", intro)
        self.assertLessEqual(len(script.split()), 30)

    def test_build_script_strips_markup(self):
        script = build_script("Title", "<p>Intro <b>text</b> here. More.</p>")
        self.assertEqual(script, "Title. Intro text here.")


class ApiTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.objects.get(depth=1)
        # A home page must host the blog index.
        home = root.add_child(
            instance=Page(title="Home", slug="home-test")
        )
        cls.index = home.add_child(
            instance=BlogIndexPage(title="Blog", slug="blog-test")
        )
        cls.article = cls.index.add_child(
            instance=BlogPage(
                title="Tracking Wild Yeast",
                slug="tracking-wild-yeast",
                introduction="Yeasts grow as single cells. Ignore this part.",
                live=True,
            )
        )
        cls.draft_article = cls.index.add_child(
            instance=BlogPage(
                title="Draft article",
                slug="draft-article",
                introduction="Draft intro.",
                live=False,
            )
        )

        # Publisher: superuser can publish any page.
        cls.publisher = User.objects.create_user(
            username="publisher", password="x", is_superuser=True, is_staff=True
        )
        cls.publisher_token = APIToken.create_token(
            user=cls.publisher, name="pub"
        )[1]

        # Non-publisher: an active user with no page permissions.
        cls.viewer = User.objects.create_user(username="viewer", password="x")
        cls.viewer_token = APIToken.create_token(user=cls.viewer, name="view")[1]

    def auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class PermissionTests(ApiTestBase):
    def test_post_requires_authentication(self):
        resp = self.client.post(f"{BASE}/pages/{self.article.id}/video/")
        self.assertEqual(resp.status_code, 401)

    def test_post_forbidden_for_non_publisher(self):
        resp = self.client.post(
            f"{BASE}/pages/{self.article.id}/video/", **self.auth(self.viewer_token)
        )
        self.assertEqual(resp.status_code, 403)

    def test_post_404_for_non_blog_page(self):
        resp = self.client.post(
            f"{BASE}/pages/{self.index.id}/video/", **self.auth(self.publisher_token)
        )
        self.assertEqual(resp.status_code, 404)

    def test_post_404_for_draft_article(self):
        resp = self.client.post(
            f"{BASE}/pages/{self.draft_article.id}/video/",
            **self.auth(self.publisher_token),
        )
        self.assertEqual(resp.status_code, 404)


class StartAndIdempotencyTests(ApiTestBase):
    @mock.patch("bakerydemo.video.api.start_async_production")
    def test_post_starts_job_and_returns_video_job_id(self, start_mock):
        resp = self.client.post(
            f"{BASE}/pages/{self.article.id}/video/",
            **self.auth(self.publisher_token),
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("videoJobId", body)
        self.assertEqual(ArticleVideo.objects.count(), 1)
        av = ArticleVideo.objects.get()
        self.assertEqual(str(av.job_id), body["videoJobId"])
        # Narration is the article's own words, capped.
        self.assertEqual(av.script, "Tracking Wild Yeast. Yeasts grow as single cells.")
        start_mock.assert_called_once()

    @mock.patch("bakerydemo.video.api.start_async_production")
    def test_second_request_is_idempotent(self, start_mock):
        url = f"{BASE}/pages/{self.article.id}/video/"
        first = self.client.post(url, **self.auth(self.publisher_token))
        second = self.client.post(url, **self.auth(self.publisher_token))
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        # Only ONE production started -> not billed twice.
        self.assertEqual(ArticleVideo.objects.count(), 1)
        start_mock.assert_called_once()

    @mock.patch("bakerydemo.video.api.start_async_production")
    def test_failed_job_is_not_auto_reproduced(self, start_mock):
        url = f"{BASE}/pages/{self.article.id}/video/"
        first = self.client.post(url, **self.auth(self.publisher_token))
        # Simulate the first attempt having failed.
        av = ArticleVideo.objects.get()
        av.status = ArticleVideo.Status.FAILED
        av.error = "boom"
        av.save()
        second = self.client.post(url, **self.auth(self.publisher_token))
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        # Never re-triggered: production still started exactly once.
        start_mock.assert_called_once()


class StatusAndDownloadTests(ApiTestBase):
    def _make_video(self, **kwargs):
        return ArticleVideo.objects.create(page=self.article, **kwargs)

    def test_status_processing(self):
        av = self._make_video(
            status=ArticleVideo.Status.PROCESSING, progress_percentage=40
        )
        resp = self.client.get(
            f"{BASE}/pages/{self.article.id}/video/{av.job_id}/",
            **self.auth(self.publisher_token),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progressPercentage"], 40)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])

    def test_status_failed_carries_error(self):
        av = self._make_video(
            status=ArticleVideo.Status.FAILED, error="provider exploded"
        )
        resp = self.client.get(
            f"{BASE}/pages/{self.article.id}/video/{av.job_id}/",
            **self.auth(self.publisher_token),
        )
        body = resp.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "provider exploded")
        self.assertIsNone(body["downloadUrl"])

    def test_status_ready_carries_download_url(self):
        from django.core.files.base import ContentFile

        av = self._make_video(
            status=ArticleVideo.Status.READY, progress_percentage=100
        )
        av.video_file.save("x.mp4", ContentFile(b"data"), save=True)
        resp = self.client.get(
            f"{BASE}/pages/{self.article.id}/video/{av.job_id}/",
            **self.auth(self.publisher_token),
        )
        body = resp.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["progressPercentage"], 100)
        self.assertIn(f"/video/{av.job_id}/download/", body["downloadUrl"])

        # And the download endpoint streams the bytes.
        dl = self.client.get(body["downloadUrl"], **self.auth(self.publisher_token))
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(b"".join(dl.streaming_content), b"data")

    def test_download_conflict_when_not_ready(self):
        av = self._make_video(status=ArticleVideo.Status.PROCESSING)
        resp = self.client.get(
            f"{BASE}/pages/{self.article.id}/video/{av.job_id}/download/",
            **self.auth(self.publisher_token),
        )
        self.assertEqual(resp.status_code, 409)

    def test_status_404_for_unknown_job(self):
        import uuid

        resp = self.client.get(
            f"{BASE}/pages/{self.article.id}/video/{uuid.uuid4()}/",
            **self.auth(self.publisher_token),
        )
        self.assertEqual(resp.status_code, 404)


@override_settings(VIDEOGEN_VISUAL_STYLE_TYPE="STOCK_FOOTAGE_PLACEHOLDER")
class ProductionPipelineTests(ApiTestBase):
    @mock.patch("bakerydemo.video.service.poll_project_export")
    @mock.patch("bakerydemo.video.service.poll_workflow_run")
    @mock.patch("bakerydemo.video.service.build_client")
    def test_produce_and_store_happy_path(self, build_mock, wf_mock, exp_mock):
        client = _mock_client()
        build_mock.return_value = client
        wf_mock.return_value = {
            "status": "succeeded",
            "project_id": "vg_proj_test",
            "progress_percentage": 100,
        }
        exp_mock.return_value = {
            "status": "succeeded",
            "export_file_id": "vg_file_test",
            "download_url": "https://provider/download",
        }

        av = ArticleVideo.objects.create(
            page=self.article, script="Tracking Wild Yeast. Yeasts grow as single cells."
        )
        produce_and_store(av)

        av.refresh_from_db()
        self.assertEqual(av.status, ArticleVideo.Status.READY)
        self.assertEqual(av.progress_percentage, 100)
        self.assertEqual(av.project_id, "vg_proj_test")
        self.assertEqual(av.export_file_id, "vg_file_test")
        self.assertTrue(av.video_file)
        # Cheap shape: script + configured stock visual style, no remix actions,
        # exactly one export.
        _, kwargs = client.workflows.script_to_video.call_args
        self.assertEqual(set(kwargs), {"script", "visual_style"})
        self.assertEqual(kwargs["visual_style"], {"type": "STOCK_FOOTAGE_PLACEHOLDER"})
        client.projects.export_project.assert_called_once()

    @mock.patch("bakerydemo.video.service.build_client")
    def test_provider_failure_becomes_typed_exception(self, build_mock):
        client = _mock_client()
        client.workflows.script_to_video.side_effect = ProviderVideoGenError(
            "quota exceeded", status=402, request_id="req_1"
        )
        build_mock.return_value = client

        av = ArticleVideo.objects.create(page=self.article, script="Title. Sentence.")
        with self.assertRaises(VideoGenServiceError) as ctx:
            produce_and_store(av)
        self.assertEqual(ctx.exception.stage, "script_to_video")
        self.assertEqual(ctx.exception.status, 402)
        self.assertEqual(ctx.exception.request_id, "req_1")


class VisualStyleConfigTests(ApiTestBase):
    @override_settings(VIDEOGEN_VISUAL_STYLE_TYPE=None)
    @mock.patch("bakerydemo.video.service.build_client")
    def test_missing_visual_style_raises_configuration_error(self, build_mock):
        build_mock.return_value = _mock_client()
        av = ArticleVideo.objects.create(page=self.article, script="Title. Sentence.")
        with self.assertRaises(VideoGenConfigurationError):
            produce_and_store(av)

    @override_settings(VIDEOGEN_VISUAL_STYLE_TYPE=None)
    @mock.patch("bakerydemo.video.service.build_client")
    def test_background_job_records_configuration_gap(self, build_mock):
        from .service import _run_job

        build_mock.return_value = _mock_client()
        av = ArticleVideo.objects.create(page=self.article, script="Title. Sentence.")
        _run_job(av.pk)
        av.refresh_from_db()
        self.assertEqual(av.status, ArticleVideo.Status.FAILED)
        self.assertIn("visual style", av.error.lower())


class GroupPublisherPermissionTests(ApiTestBase):
    """A non-superuser with an explicit 'publish' page permission is admitted."""

    @mock.patch("bakerydemo.video.api.start_async_production")
    def test_group_publisher_admitted(self, start_mock):
        group = Group.objects.create(name="Publishers")
        GroupPagePermission.objects.create(
            group=group, page=self.index, permission_type="publish"
        )
        user = User.objects.create_user(username="ed", password="x", is_staff=True)
        user.groups.add(group)
        token = APIToken.create_token(user=user, name="ed")[1]

        resp = self.client.post(
            f"{BASE}/pages/{self.article.id}/video/", **self.auth(token)
        )
        self.assertEqual(resp.status_code, 202)
