import tempfile
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from wagtail.models import APIToken, Page

from bakerydemo.video.models import VideoJobStatus
from bakerydemo.video.service import run_pipeline

from .fakes import FakeVideoGenClient


def _make_page(title="Tracking Wild Yeast", live=True):
    root = Page.get_first_root_node()
    page = Page(
        title=title,
        slug=f"{title}-{uuid.uuid4().hex[:6]}".lower().replace(" ", "-"),
        live=live,
    )
    root.add_child(instance=page)
    return page


class VideoApiTestBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("t_admin", "admin@example.com", "pw")
        self.plain = User.objects.create_user("t_plain", "plain@example.com", "pw")
        self.inactive = User.objects.create_superuser(
            "t_inactive", "inactive@example.com", "pw"
        )
        self.inactive.is_active = False
        self.inactive.save()

        _, self.admin_token = APIToken.create_token(user=self.admin, name="admin")
        _, self.plain_token = APIToken.create_token(user=self.plain, name="plain")
        _, self.inactive_token = APIToken.create_token(
            user=self.inactive, name="inactive"
        )
        self.page = _make_page()

    def _bearer(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _create_url(self, page_id=None):
        return f"/api/v3-preview/pages/{page_id or self.page.id}/video/"


class CreateVideoAuthTests(VideoApiTestBase):
    """The seeded permission model: only publishers may produce a video."""

    def test_missing_token_is_401(self):
        resp = self.client.post(self._create_url())
        self.assertEqual(resp.status_code, 401)

    def test_non_publisher_is_403(self):
        resp = self.client.post(self._create_url(), **self._bearer(self.plain_token))
        self.assertEqual(resp.status_code, 403)

    def test_inactive_user_token_is_401(self):
        resp = self.client.post(self._create_url(), **self._bearer(self.inactive_token))
        self.assertEqual(resp.status_code, 401)

    def test_bogus_token_is_401(self):
        resp = self.client.post(
            self._create_url(), **self._bearer("wagtail_not_a_real_token")
        )
        self.assertEqual(resp.status_code, 401)

    def test_publisher_is_admitted(self):
        with mock.patch("bakerydemo.video.service._launch"):
            resp = self.client.post(
                self._create_url(), **self._bearer(self.admin_token)
            )
        self.assertEqual(resp.status_code, 202)
        self.assertIn("videoJobId", resp.json())

    def test_unknown_page_is_404(self):
        with mock.patch("bakerydemo.video.service._launch"):
            resp = self.client.post(
                self._create_url(page_id=999999),
                **self._bearer(self.admin_token),
            )
        self.assertEqual(resp.status_code, 404)

    def test_unpublished_page_is_409(self):
        draft = _make_page(title="Draft", live=False)
        with mock.patch("bakerydemo.video.service._launch"):
            resp = self.client.post(
                self._create_url(page_id=draft.id),
                **self._bearer(self.admin_token),
            )
        self.assertEqual(resp.status_code, 409)


class CreateVideoIdempotencyTests(VideoApiTestBase):
    def test_second_request_is_200_with_same_job(self):
        with mock.patch("bakerydemo.video.service._launch"):
            first = self.client.post(
                self._create_url(), **self._bearer(self.admin_token)
            )
            second = self.client.post(
                self._create_url(), **self._bearer(self.admin_token)
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class VideoLifecycleTests(VideoApiTestBase):
    def test_full_flow_start_poll_download(self):
        fake = FakeVideoGenClient(workflow_polls_running=1, export_polls_running=1)

        def fake_launch(job_id, *, client_factory, runner):
            run_pipeline(str(job_id), client=fake, poll_interval=0)

        with mock.patch("bakerydemo.video.service._launch", side_effect=fake_launch):
            start = self.client.post(
                self._create_url(), **self._bearer(self.admin_token)
            )
        self.assertEqual(start.status_code, 202)
        job_id = start.json()["videoJobId"]

        # Status endpoint: ready, with a download URL and no error.
        status_url = f"{self._create_url()}{job_id}/"
        status = self.client.get(status_url, **self._bearer(self.admin_token))
        self.assertEqual(status.status_code, 200)
        body = status.json()
        self.assertEqual(body["status"], VideoJobStatus.READY)
        self.assertEqual(body["progressPercentage"], 100)
        self.assertTrue(body["downloadUrl"])
        self.assertIsNone(body["error"])

        # Download endpoint returns the stored MP4.
        download = self.client.get(
            f"{status_url}download/", **self._bearer(self.admin_token)
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Content-Type"], "video/mp4")
        self.assertEqual(b"".join(download.streaming_content), b"FAKE-MP4-BYTES")

    def test_status_requires_publish_permission(self):
        with mock.patch("bakerydemo.video.service._launch"):
            start = self.client.post(
                self._create_url(), **self._bearer(self.admin_token)
            )
        job_id = start.json()["videoJobId"]
        status_url = f"{self._create_url()}{job_id}/"

        resp = self.client.get(status_url, **self._bearer(self.plain_token))
        self.assertEqual(resp.status_code, 403)

    def test_status_unknown_job_is_404(self):
        status_url = f"{self._create_url()}{uuid.uuid4()}/"
        resp = self.client.get(status_url, **self._bearer(self.admin_token))
        self.assertEqual(resp.status_code, 404)

    def test_download_before_ready_is_404(self):
        with mock.patch("bakerydemo.video.service._launch"):
            start = self.client.post(
                self._create_url(), **self._bearer(self.admin_token)
            )
        job_id = start.json()["videoJobId"]
        resp = self.client.get(
            f"{self._create_url()}{job_id}/download/",
            **self._bearer(self.admin_token),
        )
        self.assertEqual(resp.status_code, 404)
