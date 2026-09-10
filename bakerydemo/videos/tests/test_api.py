from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos import services
from bakerydemo.videos.models import VideoJob
from bakerydemo.videos.services import run_pipeline
from bakerydemo.videos.videogen import VideoGenAuthError

User = get_user_model()


class FakeVideoGenClient:
    """Records calls and returns canned success payloads for the pipeline."""

    def __init__(self, *, fail_at=None, exc=None):
        self.fail_at = fail_at
        self.exc = exc or VideoGenAuthError("boom", status=401)
        self.scripts = []
        self.create_calls = 0
        self.export_calls = 0

    def _maybe_fail(self, step):
        if self.fail_at == step:
            raise self.exc

    def create_script_to_video(self, script, **kwargs):
        self.create_calls += 1
        self.scripts.append(script)
        self._maybe_fail("create")
        return {"workflowRunId": "run_1", "projectId": "proj_1"}

    def poll_workflow_run(self, run_id, on_progress=None, **kwargs):
        if on_progress:
            on_progress({"status": "processing", "progressPercentage": 50})
        self._maybe_fail("run")
        return {"status": "succeeded", "projectId": "proj_1", "progressPercentage": 100}

    def export_project(self, project_id, **kwargs):
        self.export_calls += 1
        self._maybe_fail("export")
        return {"exportId": "exp_1"}

    def poll_project_export(self, project_id, export_id, on_progress=None, **kwargs):
        if on_progress:
            on_progress({"status": "processing", "progressPercentage": 50})
        self._maybe_fail("export_poll")
        return {
            "status": "succeeded",
            "progressPercentage": 100,
            "downloadUrl": "https://cdn.videogen.test/video.mp4",
        }

    def get_project_export(self, project_id, export_id):
        return {
            "status": "succeeded",
            "downloadUrl": "https://cdn.videogen.test/fresh.mp4",
        }


def sync_launch(fake_client):
    """A launch_pipeline replacement that runs the pipeline inline."""

    def _launch(job_id):
        with mock.patch("bakerydemo.videos.services.connection"):
            run_pipeline(job_id, client=fake_client)

    return _launch


class VideoAPITestCase(TestCase):
    def setUp(self):
        root = Page.get_first_root_node()
        self.index = BlogIndexPage(title="Blog", slug="blog-videos")
        root.add_child(instance=self.index)

        self.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="tracking-wild-yeast",
            introduction="Wild yeast is everywhere. Extra text is ignored.",
            live=True,
        )
        self.index.add_child(instance=self.article)

        self.draft = BlogPage(
            title="Draft Article",
            slug="draft-article",
            introduction="Not published yet.",
            live=False,
        )
        self.index.add_child(instance=self.draft)

        # Users.
        self.publisher = User.objects.create_superuser(
            "pub", "pub@example.test", "pw"
        )
        self.non_publisher = User.objects.create_user(
            "editor_like", "ed@example.test", "pw"
        )
        self.inactive = User.objects.create_superuser(
            "inactive_su", "in@example.test", "pw"
        )
        self.inactive.is_active = False
        self.inactive.save()
        self.revoked_user = User.objects.create_superuser(
            "revoked", "rv@example.test", "pw"
        )

        from wagtail.models import APIToken

        _, self.publisher_token = APIToken.create_token(
            user=self.publisher, name="pub"
        )
        _, self.non_publisher_token = APIToken.create_token(
            user=self.non_publisher, name="np"
        )
        _, self.inactive_token = APIToken.create_token(
            user=self.inactive, name="ia"
        )
        revoked, self.revoked_token = APIToken.create_token(
            user=self.revoked_user, name="rv"
        )
        revoked.revoke()

    def post_video(self, page_id, token=None):
        headers = {}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.post(f"/api/v3-preview/pages/{page_id}/video/", **headers)

    def get_video(self, page_id, job_id, token=None):
        headers = {}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.get(
            f"/api/v3-preview/pages/{page_id}/video/{job_id}/", **headers
        )


class PermissionTests(VideoAPITestCase):
    def test_publisher_can_start(self):
        with mock.patch.object(services, "launch_pipeline", mock.Mock()):
            resp = self.post_video(self.article.id, self.publisher_token)
        self.assertEqual(resp.status_code, 202)
        self.assertIn("videoJobId", resp.json())

    def test_non_publisher_forbidden(self):
        with mock.patch.object(services, "launch_pipeline", mock.Mock()) as launch:
            resp = self.post_video(self.article.id, self.non_publisher_token)
        self.assertEqual(resp.status_code, 403)
        launch.assert_not_called()
        self.assertFalse(VideoJob.objects.exists())

    def test_missing_token_unauthorized(self):
        resp = self.post_video(self.article.id, token=None)
        self.assertEqual(resp.status_code, 401)

    def test_inactive_user_unauthorized(self):
        resp = self.post_video(self.article.id, self.inactive_token)
        self.assertEqual(resp.status_code, 401)

    def test_revoked_token_unauthorized(self):
        resp = self.post_video(self.article.id, self.revoked_token)
        self.assertEqual(resp.status_code, 401)


class ValidationTests(VideoAPITestCase):
    def test_non_blog_page_rejected(self):
        with mock.patch.object(services, "launch_pipeline", mock.Mock()):
            resp = self.post_video(self.index.id, self.publisher_token)
        self.assertEqual(resp.status_code, 422)

    def test_unpublished_article_rejected(self):
        with mock.patch.object(services, "launch_pipeline", mock.Mock()):
            resp = self.post_video(self.draft.id, self.publisher_token)
        self.assertEqual(resp.status_code, 422)

    def test_unknown_page_404(self):
        resp = self.post_video(999999, self.publisher_token)
        self.assertEqual(resp.status_code, 404)


class IdempotencyTests(VideoAPITestCase):
    def test_second_request_reuses_job_and_does_not_rebill(self):
        fake = FakeVideoGenClient()
        with mock.patch.object(services, "launch_pipeline", sync_launch(fake)):
            first = self.post_video(self.article.id, self.publisher_token)
            second = self.post_video(self.article.id, self.publisher_token)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(VideoJob.objects.count(), 1)
        # Only the first request produced a video / export.
        self.assertEqual(fake.create_calls, 1)
        self.assertEqual(fake.export_calls, 1)

    def test_script_is_title_plus_first_intro_sentence(self):
        fake = FakeVideoGenClient()
        with mock.patch.object(services, "launch_pipeline", sync_launch(fake)):
            self.post_video(self.article.id, self.publisher_token)
        self.assertEqual(
            fake.scripts[0], "Tracking Wild Yeast. Wild yeast is everywhere."
        )


class StatusFlowTests(VideoAPITestCase):
    def test_full_flow_reports_ready_with_download_url(self):
        fake = FakeVideoGenClient()
        with mock.patch.object(services, "launch_pipeline", sync_launch(fake)):
            start = self.post_video(self.article.id, self.publisher_token)
        job_id = start.json()["videoJobId"]

        with mock.patch.object(services, "build_client", return_value=fake):
            resp = self.get_video(self.article.id, job_id, self.publisher_token)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["progressPercentage"], 100)
        # get_project_export re-signs the URL on read.
        self.assertEqual(body["downloadUrl"], "https://cdn.videogen.test/fresh.mp4")
        self.assertIsNone(body["error"])

    def test_failure_is_reported_with_error(self):
        fake = FakeVideoGenClient(fail_at="run", exc=VideoGenAuthError("denied"))
        with mock.patch.object(services, "launch_pipeline", sync_launch(fake)):
            start = self.post_video(self.article.id, self.publisher_token)
        job_id = start.json()["videoJobId"]

        resp = self.get_video(self.article.id, job_id, self.publisher_token)
        body = resp.json()
        self.assertEqual(body["status"], "failed")
        self.assertIn("denied", body["error"])
        self.assertIsNone(body["downloadUrl"])

    def test_get_requires_publish_permission(self):
        fake = FakeVideoGenClient()
        with mock.patch.object(services, "launch_pipeline", sync_launch(fake)):
            start = self.post_video(self.article.id, self.publisher_token)
        job_id = start.json()["videoJobId"]

        resp = self.get_video(self.article.id, job_id, self.non_publisher_token)
        self.assertEqual(resp.status_code, 403)

    def test_get_unknown_job_404(self):
        resp = self.get_video(
            self.article.id,
            "00000000-0000-0000-0000-000000000000",
            self.publisher_token,
        )
        self.assertEqual(resp.status_code, 404)

    def test_get_job_from_other_page_404(self):
        fake = FakeVideoGenClient()
        with mock.patch.object(services, "launch_pipeline", sync_launch(fake)):
            start = self.post_video(self.article.id, self.publisher_token)
        job_id = start.json()["videoJobId"]

        # Same (valid) job id but under a different page id must not resolve.
        other = BlogPage(title="Other", slug="other-article", live=True)
        self.index.add_child(instance=other)
        resp = self.get_video(other.id, job_id, self.publisher_token)
        self.assertEqual(resp.status_code, 404)
