"""Tests for the article-video integration.

The VideoGen provider is mocked throughout, so these tests never contact the
real API and never incur cost. They cover narration assembly, authentication,
per-page publish permission, idempotency, the full success pipeline, and
provider-failure handling.
"""

from __future__ import annotations

import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

from . import services
from .exceptions import VideoGenWorkflowError
from .models import VideoJob, VideoJobStatus
from .narration import build_narration_script

User = get_user_model()

# A placeholder visual style for tests. The real stock-footage value is not
# documented by the VideoGen `api` skill (see docs/videogen.md); tests only need
# *some* configured value so the pipeline proceeds past the preflight check.
TEST_VISUAL_STYLE = {"type": "TEST_STOCK"}

INTRO = (
    "Yeasts, with their single-celled growth habit, can be contrasted with "
    "molds, which grow hyphae. A second sentence that must not be narrated."
)


class FakeVideoGenClient:
    """A stand-in for :class:`~bakerydemo.videos.client.VideoGenClient`.

    Records the calls made so tests can assert the provider is exercised (or
    not) exactly as expected, without any network access or billing.
    """

    instances: list[FakeVideoGenClient] = []

    def __init__(self, *args, **kwargs):
        self.script = None
        self.export_calls = 0
        FakeVideoGenClient.instances.append(self)

    def create_script_to_video(self, script, *, extra=None):
        self.script = script
        return {"workflowRunId": "vg_work_test", "projectId": "vg_proj_test"}

    def wait_for_workflow(self, run_id, *, on_progress=None, deadline=None):
        if on_progress:
            on_progress(100.0)
        return {"status": "succeeded", "projectId": "vg_proj_test"}

    def export_project(self, project_id, *, extra=None):
        self.export_calls += 1
        return {"exportId": "vg_exp_test"}

    def wait_for_export(self, project_id, export_id, *, on_progress=None, deadline=None):
        if on_progress:
            on_progress(100.0)
        return {
            "status": "succeeded",
            "downloadUrl": "https://example.invalid/video.mp4",
            "exportFileId": "vg_file_test",
        }

    def download_to(self, url, target):
        target.write(b"FAKE-MP4-BYTES")
        return 14

    def close(self):
        pass


def _bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class NarrationTests(TestCase):
    def test_uses_title_and_first_sentence_only(self):
        page = BlogPage(title="Tracking Wild Yeast", introduction=INTRO)
        script = build_narration_script(page, max_words=30)
        self.assertEqual(
            script,
            "Tracking Wild Yeast. Yeasts, with their single-celled growth "
            "habit, can be contrasted with molds, which grow hyphae.",
        )
        self.assertNotIn("second sentence", script)

    def test_word_cap_is_enforced(self):
        page = BlogPage(title="A B C D E", introduction="one two three four five six.")
        script = build_narration_script(page, max_words=4)
        self.assertEqual(len(script.split()), 4)

    def test_handles_empty_introduction(self):
        page = BlogPage(title="Just a title", introduction="")
        self.assertEqual(build_narration_script(page), "Just a title.")


class ArticleVideoAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        index = BlogIndexPage(title="Blog", slug="blog-test")
        root.add_child(instance=index)
        cls.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="tracking-wild-yeast",
            introduction=INTRO,
            live=True,
        )
        index.add_child(instance=cls.article)

        # A draft (unpublished) article, to prove the endpoint only serves
        # published content.
        cls.draft = BlogPage(
            title="Draft", slug="draft", introduction="x.", live=False
        )
        index.add_child(instance=cls.draft)

        cls.publisher = User.objects.create_superuser(
            "pub", "pub@example.com", "pw"
        )
        _, cls.publisher_token = APIToken.create_token(
            user=cls.publisher, name="pub"
        )

        cls.plain = User.objects.create_user(
            "plain", "plain@example.com", "pw", is_active=True
        )
        _, cls.plain_token = APIToken.create_token(user=cls.plain, name="plain")

    def setUp(self):
        FakeVideoGenClient.instances = []

    def _url(self, page_id):
        return f"/api/v3-preview/pages/{page_id}/video/"

    # -- authentication & permission -----------------------------------

    def test_requires_authentication(self):
        resp = self.client.post(self._url(self.article.id))
        self.assertEqual(resp.status_code, 401)

    def test_publisher_permitted(self):
        with mock.patch.object(services, "enqueue_video_job"):
            resp = self.client.post(
                self._url(self.article.id), **_bearer(self.publisher_token)
            )
        self.assertEqual(resp.status_code, 202)
        self.assertIn("videoJobId", resp.json())

    def test_non_publisher_forbidden(self):
        resp = self.client.post(
            self._url(self.article.id), **_bearer(self.plain_token)
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(VideoJob.objects.exists())

    def test_revoked_token_rejected(self):
        token_obj, raw = APIToken.create_token(user=self.publisher, name="revoked")
        token_obj.revoke()
        resp = self.client.post(self._url(self.article.id), **_bearer(raw))
        self.assertEqual(resp.status_code, 401)

    def test_unpublished_article_not_found(self):
        resp = self.client.post(
            self._url(self.draft.id), **_bearer(self.publisher_token)
        )
        self.assertEqual(resp.status_code, 404)

    def test_non_article_page_not_found(self):
        resp = self.client.post(
            self._url(Page.get_first_root_node().id), **_bearer(self.publisher_token)
        )
        self.assertEqual(resp.status_code, 404)

    # -- idempotency ---------------------------------------------------

    def test_second_request_reuses_job_and_does_not_rebill(self):
        with mock.patch.object(services, "enqueue_video_job") as enqueue:
            first = self.client.post(
                self._url(self.article.id), **_bearer(self.publisher_token)
            )
            second = self.client.post(
                self._url(self.article.id), **_bearer(self.publisher_token)
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            first.json()["videoJobId"], second.json()["videoJobId"]
        )
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        # Only the first (newly created) job is enqueued for production.
        self.assertEqual(enqueue.call_count, 1)

    # -- full pipeline (mocked provider) -------------------------------

    def test_full_pipeline_produces_downloadable_video(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(
            MEDIA_ROOT=media_root, VIDEOGEN_VISUAL_STYLE=TEST_VISUAL_STYLE
        ), mock.patch.object(services, "enqueue_video_job"):
            create = self.client.post(
                self._url(self.article.id), **_bearer(self.publisher_token)
            )
            job_id = create.json()["videoJobId"]

            with mock.patch.object(services, "VideoGenClient", FakeVideoGenClient):
                services.run_video_job(job_id)

            job = VideoJob.objects.get(pk=job_id)
            self.assertEqual(job.status, VideoJobStatus.READY)
            self.assertEqual(job.progress_percentage, 100)
            self.assertTrue(job.video_file)
            self.assertEqual(job.script, build_narration_script(self.article))
            # Exactly one export was requested for the one video.
            self.assertEqual(FakeVideoGenClient.instances[0].export_calls, 1)

            status = self.client.get(
                f"{self._url(self.article.id)}{job_id}/",
                **_bearer(self.publisher_token),
            )
            body = status.json()
            self.assertEqual(body["status"], "ready")
            self.assertEqual(body["progressPercentage"], 100)
            self.assertTrue(body["downloadUrl"].endswith(f"/{job_id}/download/"))
            self.assertIsNone(body["error"])

            download = self.client.get(
                f"{self._url(self.article.id)}{job_id}/download/",
                **_bearer(self.publisher_token),
            )
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download["Content-Type"], "video/mp4")
            self.assertEqual(
                b"".join(download.streaming_content), b"FAKE-MP4-BYTES"
            )

    def test_provider_failure_is_recorded(self):
        with mock.patch.object(services, "enqueue_video_job"):
            create = self.client.post(
                self._url(self.article.id), **_bearer(self.publisher_token)
            )
        job_id = create.json()["videoJobId"]

        class FailingClient(FakeVideoGenClient):
            def wait_for_workflow(self, *a, **k):
                raise VideoGenWorkflowError("VideoGen workflow run failed.")

        with override_settings(VIDEOGEN_VISUAL_STYLE=TEST_VISUAL_STYLE), mock.patch.object(
            services, "VideoGenClient", FailingClient
        ):
            services.run_video_job(job_id)

        job = VideoJob.objects.get(pk=job_id)
        self.assertEqual(job.status, VideoJobStatus.FAILED)
        self.assertIn("failed", job.error.lower())

        status = self.client.get(
            f"{self._url(self.article.id)}{job_id}/",
            **_bearer(self.publisher_token),
        )
        self.assertEqual(status.json()["status"], "failed")
        self.assertTrue(status.json()["error"])

    def test_missing_visual_style_fails_fast_without_calling_provider(self):
        with mock.patch.object(services, "enqueue_video_job"):
            create = self.client.post(
                self._url(self.article.id), **_bearer(self.publisher_token)
            )
        job_id = create.json()["videoJobId"]

        # No VIDEOGEN_VISUAL_STYLE configured: the pipeline must fail fast with a
        # clear configuration error and must not reach the provider.
        with override_settings(VIDEOGEN_VISUAL_STYLE=None), mock.patch.object(
            services, "VideoGenClient", FakeVideoGenClient
        ):
            services.run_video_job(job_id)

        self.assertEqual(FakeVideoGenClient.instances, [])
        job = VideoJob.objects.get(pk=job_id)
        self.assertEqual(job.status, VideoJobStatus.FAILED)
        self.assertIn("visual style", job.error.lower())

    def test_download_404_until_ready(self):
        with mock.patch.object(services, "enqueue_video_job"):
            create = self.client.post(
                self._url(self.article.id), **_bearer(self.publisher_token)
            )
        job_id = create.json()["videoJobId"]
        resp = self.client.get(
            f"{self._url(self.article.id)}{job_id}/download/",
            **_bearer(self.publisher_token),
        )
        self.assertEqual(resp.status_code, 404)
