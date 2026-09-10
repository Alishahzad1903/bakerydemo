"""Tests for the article-video integration.

None of these tests call the real VideoGen API. Provider interactions are
exercised through a fake client so the full production flow can be verified
without producing (or being billed for) a video.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.test import RequestFactory, TestCase, override_settings
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

from . import production
from .models import VideoJob
from .narration import MAX_NARRATION_WORDS, build_narration_script, first_sentence
from .videogen import VideoGenCapabilityUnavailable, VideoGenConfigurationError
from .videogen.exceptions import VideoGenAPIError

# A configured, spend-compliant-shaped stand-in for the two undocumented knobs.
# Values are placeholders used only to exercise the flow; they are NOT claims
# about the real VideoGen API.
_TEST_VISUAL_STYLE = {"type": "STOCK_VIDEO_PLACEHOLDER"}
_TEST_EXPORT_OPTIONS = {"note": "placeholder export options for tests"}
CONFIGURED = override_settings(
    VIDEOGEN_API_KEY="test-key",
    VIDEOGEN_VISUAL_STYLE=_TEST_VISUAL_STYLE,
    VIDEOGEN_EXPORT_OPTIONS=_TEST_EXPORT_OPTIONS,
)


class NarrationTests(TestCase):
    def test_first_sentence(self):
        self.assertEqual(first_sentence("One. Two. Three."), "One.")
        self.assertEqual(first_sentence("No terminator here"), "No terminator here")
        self.assertEqual(first_sentence("   "), "")

    def test_script_is_title_plus_first_intro_sentence(self):
        page = SimpleNamespace(
            title="Tracking Wild Yeast",
            introduction=(
                "Yeasts, with their single-celled growth habit, can be "
                "contrasted with molds, which grow hyphae. Fungal species that "
                "can take both forms are called dimorphic fungi."
            ),
        )
        script = build_narration_script(page)
        self.assertEqual(
            script,
            "Tracking Wild Yeast. Yeasts, with their single-celled growth "
            "habit, can be contrasted with molds, which grow hyphae.",
        )
        # The body / second sentence is intentionally excluded.
        self.assertNotIn("dimorphic", script)

    def test_word_ceiling_is_enforced(self):
        page = SimpleNamespace(
            title="A Title",
            introduction=" ".join(f"word{i}" for i in range(100)) + ".",
        )
        script = build_narration_script(page)
        self.assertLessEqual(len(script.split()), MAX_NARRATION_WORDS)

    def test_missing_introduction_uses_title_only(self):
        page = SimpleNamespace(title="Just A Title", introduction="")
        self.assertEqual(build_narration_script(page), "Just A Title.")


class ConfigGuardTests(TestCase):
    @override_settings(
        VIDEOGEN_API_KEY="", VIDEOGEN_VISUAL_STYLE=None, VIDEOGEN_EXPORT_OPTIONS=None
    )
    def test_missing_api_key_raises_configuration_error(self):
        with self.assertRaises(VideoGenConfigurationError):
            production.resolve_production_config()

    @override_settings(
        VIDEOGEN_API_KEY="test-key",
        VIDEOGEN_VISUAL_STYLE=None,
        VIDEOGEN_EXPORT_OPTIONS=None,
    )
    def test_unset_visual_style_reports_capability_gap(self):
        with self.assertRaises(VideoGenCapabilityUnavailable):
            production.resolve_production_config()

    @override_settings(
        VIDEOGEN_API_KEY="test-key",
        VIDEOGEN_VISUAL_STYLE=_TEST_VISUAL_STYLE,
        VIDEOGEN_EXPORT_OPTIONS=None,
    )
    def test_unset_export_options_reports_capability_gap(self):
        with self.assertRaises(VideoGenCapabilityUnavailable):
            production.resolve_production_config()

    @CONFIGURED
    def test_configured_returns_config(self):
        config = production.resolve_production_config()
        self.assertEqual(config.visual_style, _TEST_VISUAL_STYLE)
        self.assertEqual(config.export_options, _TEST_EXPORT_OPTIONS)


class FakeClient:
    """Minimal stand-in for VideoGenClient covering the production flow."""

    def __init__(self, *, video_bytes=b"MP4DATA"):
        self.video_bytes = video_bytes
        self.calls = []
        self.export_calls = 0
        self.script_calls = 0

    def create_script_to_video(
        self, *, script, visual_style, quality=None, remix_actions=None
    ):
        self.script_calls += 1
        self.calls.append(("script_to_video", script, visual_style))
        return {"workflowRunId": "vg_work_test", "projectId": "vg_proj_test"}

    def poll_workflow_run(self, run_id, *, on_progress=None, **kw):
        if on_progress:
            on_progress({"progressPercentage": 100, "status": "running"})
        return {"status": "succeeded", "projectId": "vg_proj_test"}

    def export_project(self, project_id, options=None):
        self.export_calls += 1
        return {"exportId": "vg_export_test"}

    def poll_project_export(self, project_id, export_id, *, on_progress=None, **kw):
        if on_progress:
            on_progress({"progressPercentage": 100, "status": "running"})
        return {
            "status": "succeeded",
            "downloadUrl": "https://signed.example/vid.mp4",
            "exportFileId": "vg_file_test",
        }

    def hydrate_file(self, file_id):
        return {"downloadSource": {"url": "https://signed.example/vid.mp4"}}

    def download_bytes(self, url):
        return self.video_bytes


class ProductionFlowTests(TestCase):
    def _make_article(self, title="Test Article", live=True):
        root = Page.get_first_root_node()
        index = root.add_child(instance=BlogIndexPage(title="Blog", slug="blog-test"))
        page = index.add_child(
            instance=BlogPage(
                title=title,
                slug="test-article",
                introduction="An interesting first sentence. A second one.",
                live=live,
            )
        )
        return page

    @CONFIGURED
    def test_happy_path_produces_and_stores_mp4(self):
        page = self._make_article()
        job = VideoJob.objects.create(page=page, script=build_narration_script(page))
        fake = FakeClient(video_bytes=b"FINISHED-MP4")

        with self.settings():
            production.get_client = lambda **kw: fake  # type: ignore[assignment]
            try:
                production._run_production(str(job.id))
            finally:
                # restore
                from .videogen import get_client as real_get_client

                production.get_client = real_get_client  # type: ignore[assignment]

        job.refresh_from_db()
        self.assertEqual(job.status, VideoJob.Status.SUCCEEDED)
        self.assertEqual(job.progress_percentage, 100)
        self.assertTrue(job.video_file)
        self.assertEqual(job.video_file.read(), b"FINISHED-MP4")
        self.assertEqual(job.workflow_run_id, "vg_work_test")
        self.assertEqual(fake.script_calls, 1)
        self.assertEqual(fake.export_calls, 1)

    @CONFIGURED
    def test_failure_is_recorded_on_job(self):
        page = self._make_article()
        job = VideoJob.objects.create(page=page, script="hi")

        class BoomClient(FakeClient):
            def create_script_to_video(self, **kw):
                raise VideoGenAPIError("boom", status_code=500)

        production.get_client = lambda **kw: BoomClient()  # type: ignore[assignment]
        try:
            production._run_production(str(job.id))
        finally:
            from .videogen import get_client as real_get_client

            production.get_client = real_get_client  # type: ignore[assignment]

        job.refresh_from_db()
        self.assertEqual(job.status, VideoJob.Status.FAILED)
        self.assertIn("boom", job.error)


class ApiEndpointTests(TestCase):
    """End-to-end endpoint behaviour with a superuser token and mocked production."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from wagtail.models import APIToken

        User = get_user_model()
        self.user = User.objects.create_superuser(
            "apiuser", "apiuser@example.com", "pw"
        )
        # Create a token; the plaintext is only available from this return value.
        self.token, self.raw_token = APIToken.create_token(user=self.user, name="test")
        self.factory = RequestFactory()

        root = Page.get_first_root_node()
        index = root.add_child(instance=BlogIndexPage(title="Blog", slug="blog-x"))
        self.article = index.add_child(
            instance=BlogPage(
                title="Yeast",
                slug="yeast",
                introduction="A first sentence here. Second.",
                live=True,
            )
        )
        self._started = []
        from . import api as api_module

        self._api_module = api_module
        self._orig_start = api_module.start_production
        api_module.start_production = lambda job_id: self._started.append(job_id)

    def tearDown(self):
        self._api_module.start_production = self._orig_start

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.raw_token}"}

    @CONFIGURED
    def test_post_is_idempotent(self):
        url = f"/api/v3-preview/pages/{self.article.id}/video/"
        r1 = self.client.post(url, **self._auth())
        self.assertEqual(r1.status_code, 202)
        job_id = r1.json()["videoJobId"]

        r2 = self.client.post(url, **self._auth())
        self.assertEqual(r2.status_code, 202)
        self.assertEqual(r2.json()["videoJobId"], job_id)

        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        self.assertEqual(len(self._started), 1)  # production kicked off once

    def test_post_requires_auth(self):
        url = f"/api/v3-preview/pages/{self.article.id}/video/"
        r = self.client.post(url)
        self.assertEqual(r.status_code, 401)

    @CONFIGURED
    def test_status_reports_fields(self):
        job = VideoJob.objects.create(
            page=self.article,
            status=VideoJob.Status.RUNNING,
            progress_percentage=42,
        )
        url = f"/api/v3-preview/pages/{self.article.id}/video/{job.id}/"
        r = self.client.get(url, **self._auth())
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["progressPercentage"], 42)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])
