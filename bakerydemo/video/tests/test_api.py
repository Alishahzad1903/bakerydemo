import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.video.models import VideoJob

from .fakes import FakeVideoGenClient, project_export, workflow_run

User = get_user_model()

GET_CLIENT = "bakerydemo.video.services.get_client"


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ArticleVideoAPITests(TestCase):
    def setUp(self):
        root = Page.objects.get(depth=1)
        self.index = BlogIndexPage(title="Blog", slug="blog", introduction="")
        root.add_child(instance=self.index)
        self.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="tracking-wild-yeast",
            introduction=(
                "Yeasts, with their single-celled growth habit, can be contrasted "
                "with molds, which grow hyphae. A second body sentence."
            ),
            live=True,
        )
        self.index.add_child(instance=self.article)

        self.superuser = User.objects.create_superuser("su", "su@example.com", "pw")
        self.editor = User.objects.create_user("ed", "ed@example.com", "pw")
        self.inactive = User.objects.create_superuser("ia", "ia@example.com", "pw")
        self.inactive.is_active = False
        self.inactive.save()

        _, self.su_token = APIToken.create_token(user=self.superuser, name="su")
        _, self.ed_token = APIToken.create_token(user=self.editor, name="ed")
        _, self.ia_token = APIToken.create_token(user=self.inactive, name="ia")
        revoked = APIToken.create_token(user=self.superuser, name="revoked")[0]
        revoked.revoke()
        self.revoked_token = APIToken.generate_token()  # never stored -> unknown

    # -- helpers ---------------------------------------------------------------

    def _create_url(self, page=None):
        page = page or self.article
        return f"/api/v3-preview/pages/{page.id}/video/"

    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _post(self, token, page=None):
        return self.client.post(self._create_url(page), **self._auth(token))

    # -- authentication & permission matrix ------------------------------------

    def test_anonymous_is_unauthorized(self):
        self.assertEqual(self.client.post(self._create_url()).status_code, 401)

    def test_invalid_token_is_unauthorized(self):
        self.assertEqual(self._post("wagtail_not_a_real_token").status_code, 401)

    def test_inactive_user_is_unauthorized(self):
        self.assertEqual(self._post(self.ia_token).status_code, 401)

    def test_editor_without_publish_is_forbidden(self):
        with mock.patch(GET_CLIENT, return_value=FakeVideoGenClient()):
            response = self._post(self.ed_token)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(VideoJob.objects.exists())

    def test_publisher_is_allowed(self):
        with mock.patch(GET_CLIENT, return_value=FakeVideoGenClient()):
            response = self._post(self.su_token)
        self.assertEqual(response.status_code, 201)
        self.assertIn("videoJobId", response.json())

    # -- idempotency -----------------------------------------------------------

    def test_requesting_twice_reuses_one_job(self):
        fake = FakeVideoGenClient()
        with mock.patch(GET_CLIENT, return_value=fake):
            first = self._post(self.su_token)
            second = self._post(self.su_token)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(VideoJob.objects.count(), 1)
        # One video produced, billed once.
        self.assertEqual(fake.create_calls, 1)

    def test_narration_sent_is_title_and_first_sentence(self):
        fake = FakeVideoGenClient()
        with mock.patch(GET_CLIENT, return_value=fake):
            self._post(self.su_token)
        self.assertEqual(
            fake.last_script,
            "Tracking Wild Yeast. Yeasts, with their single-celled growth habit, "
            "can be contrasted with molds, which grow hyphae.",
        )
        self.assertNotIn("second body sentence", fake.last_script.lower())

    # -- page validation -------------------------------------------------------

    def test_non_blog_page_is_rejected(self):
        with mock.patch(GET_CLIENT, return_value=FakeVideoGenClient()):
            response = self._post(self.su_token, page=self.index)
        self.assertEqual(response.status_code, 422)

    def test_unpublished_article_is_rejected(self):
        self.article.live = False
        self.article.save()
        with mock.patch(GET_CLIENT, return_value=FakeVideoGenClient()):
            response = self._post(self.su_token)
        self.assertEqual(response.status_code, 422)

    def test_missing_page_is_not_found(self):
        response = self.client.post(
            "/api/v3-preview/pages/999999/video/", **self._auth(self.su_token)
        )
        self.assertEqual(response.status_code, 404)

    # -- status lifecycle & download -------------------------------------------

    def _status_url(self, job_id, page=None):
        page = page or self.article
        return f"/api/v3-preview/pages/{page.id}/video/{job_id}/"

    def test_full_lifecycle_to_ready_and_download(self):
        fake = FakeVideoGenClient(
            workflow_runs=[
                workflow_run("running", 50),
                workflow_run("succeeded", 100),
            ],
            exports=[
                project_export("running", 40),
                project_export(
                    "succeeded", 100, download_url="https://signed/video.mp4"
                ),
            ],
        )
        with mock.patch(GET_CLIENT, return_value=fake):
            job_id = self._post(self.su_token).json()["videoJobId"]

            # First poll: build still running.
            body = self.client.get(
                self._status_url(job_id), **self._auth(self.su_token)
            ).json()
            self.assertEqual(body["status"], "processing")
            self.assertIsNone(body["downloadUrl"])
            self.assertIsNone(body["error"])

            # Poll until ready.
            for _ in range(5):
                body = self.client.get(
                    self._status_url(job_id), **self._auth(self.su_token)
                ).json()
                if body["status"] != "processing":
                    break

            self.assertEqual(body["status"], "ready")
            self.assertEqual(body["progressPercentage"], 100)
            self.assertTrue(body["downloadUrl"].endswith("/download/"))

            # Exactly one export (quality omitted -> provider default).
            self.assertEqual(fake.export_calls, 1)
            self.assertIsNone(fake.last_quality)
            self.assertEqual(fake.download_calls, 1)

            # The download streams the stored MP4.
            download = self.client.get(
                f"{self._status_url(job_id)}download/", **self._auth(self.su_token)
            )
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download["Content-Type"], "video/mp4")
            self.assertEqual(b"".join(download.streaming_content), b"FAKE-MP4-BYTES")

    def test_workflow_failure_surfaces_error(self):
        fake = FakeVideoGenClient(
            workflow_runs=[workflow_run("failed", 30, error="script rejected")]
        )
        with mock.patch(GET_CLIENT, return_value=fake):
            job_id = self._post(self.su_token).json()["videoJobId"]
            body = self.client.get(
                self._status_url(job_id), **self._auth(self.su_token)
            ).json()

        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "script rejected")
        self.assertIsNone(body["downloadUrl"])
        # A failed build never triggers an export.
        self.assertEqual(fake.export_calls, 0)

    def test_export_failure_surfaces_error(self):
        fake = FakeVideoGenClient(
            workflow_runs=[workflow_run("succeeded", 100)],
            exports=[project_export("failed", 60, error="render crashed")],
        )
        with mock.patch(GET_CLIENT, return_value=fake):
            job_id = self._post(self.su_token).json()["videoJobId"]
            for _ in range(5):
                body = self.client.get(
                    self._status_url(job_id), **self._auth(self.su_token)
                ).json()
                if body["status"] != "processing":
                    break
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "render crashed")

    def test_download_before_ready_is_not_found(self):
        fake = FakeVideoGenClient(workflow_runs=[workflow_run("running", 20)])
        with mock.patch(GET_CLIENT, return_value=fake):
            job_id = self._post(self.su_token).json()["videoJobId"]
            response = self.client.get(
                f"{self._status_url(job_id)}download/", **self._auth(self.su_token)
            )
        self.assertEqual(response.status_code, 404)

    def test_status_for_job_of_other_page_is_not_found(self):
        other = BlogPage(title="Other", slug="other", introduction="Intro.", live=True)
        self.index.add_child(instance=other)
        fake = FakeVideoGenClient()
        with mock.patch(GET_CLIENT, return_value=fake):
            job_id = self._post(self.su_token).json()["videoJobId"]
            # Same job id, but under a different page → 404.
            response = self.client.get(
                self._status_url(job_id, page=other), **self._auth(self.su_token)
            )
        self.assertEqual(response.status_code, 404)
