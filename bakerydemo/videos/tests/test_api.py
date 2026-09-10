from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos import service
from bakerydemo.videos.models import VideoJob, VideoJobState

from .fakes import FakeVideoGen, workflow_run

User = get_user_model()


class VideoApiTestBase(TestCase):
    def setUp(self):
        root = Page.get_first_root_node()
        self.index = BlogIndexPage(title="Blog", slug="blog-api")
        root.add_child(instance=self.index)
        self.article = self._make_page(live=True)
        self.draft = self._make_page(live=False, slug="draft-article", title="Draft")

        self.superuser = User.objects.create_superuser(
            username="pub", email="pub@example.com", password="x"
        )
        self.plain = User.objects.create_user(
            username="plain", email="plain@example.com", password="x"
        )

        self.fake = FakeVideoGen()
        patcher = mock.patch.object(service, "vg", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_page(self, *, live=True, slug="wild-yeast", title="Tracking Wild Yeast"):
        page = BlogPage(
            title=title,
            slug=slug,
            introduction="Yeasts and molds are different. Second sentence.",
            live=live,
        )
        self.index.add_child(instance=page)
        return page

    def _auth(self, user):
        _token, plaintext = APIToken.create_token(user=user, name="t")
        return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}"}

    def _create_url(self, page):
        return reverse("wagtailapi_v3:pages_video_create", kwargs={"page_id": page.pk})


class CreateVideoAuthTests(VideoApiTestBase):
    def test_requires_authentication(self):
        resp = self.client.post(self._create_url(self.article))
        self.assertEqual(resp.status_code, 401)

    def test_forbids_caller_without_publish_permission(self):
        resp = self.client.post(self._create_url(self.article), **self._auth(self.plain))
        self.assertEqual(resp.status_code, 403)

    def test_allows_publisher(self):
        resp = self.client.post(self._create_url(self.article), **self._auth(self.superuser))
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertIn("videoJobId", data)
        self.assertEqual(data["status"], "processing")
        self.assertEqual(self.fake.calls["start_script_to_video"], 1)


class CreateVideoBehaviourTests(VideoApiTestBase):
    def test_rejects_unpublished_article(self):
        resp = self.client.post(self._create_url(self.draft), **self._auth(self.superuser))
        self.assertEqual(resp.status_code, 422)

    def test_404_for_missing_page(self):
        url = reverse("wagtailapi_v3:pages_video_create", kwargs={"page_id": 999999})
        resp = self.client.post(url, **self._auth(self.superuser))
        self.assertEqual(resp.status_code, 404)

    def test_idempotent_second_post_returns_existing(self):
        auth = self._auth(self.superuser)
        first = self.client.post(self._create_url(self.article), **auth)
        second = self.client.post(self._create_url(self.article), **auth)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        self.assertEqual(self.fake.calls["start_script_to_video"], 1)


class StatusAndDownloadTests(VideoApiTestBase):
    def _start(self):
        auth = self._auth(self.superuser)
        resp = self.client.post(self._create_url(self.article), **auth)
        return resp.json()["videoJobId"], auth

    def test_status_reports_processing(self):
        job_id, auth = self._start()
        self.fake.workflow_runs = [workflow_run("running", progress=20.0)]
        url = reverse(
            "wagtailapi_v3:pages_video_status",
            kwargs={"page_id": self.article.pk, "video_job_id": job_id},
        )
        resp = self.client.get(url, **auth)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "processing")
        self.assertEqual(data["progressPercentage"], 10.0)
        self.assertIsNone(data["downloadUrl"])
        self.assertIsNone(data["error"])

    def test_status_requires_publish_permission(self):
        job_id, _ = self._start()
        url = reverse(
            "wagtailapi_v3:pages_video_status",
            kwargs={"page_id": self.article.pk, "video_job_id": job_id},
        )
        resp = self.client.get(url, **self._auth(self.plain))
        self.assertEqual(resp.status_code, 403)

    def test_download_404_until_ready(self):
        job_id, auth = self._start()
        url = reverse(
            "wagtailapi_v3:pages_video_download",
            kwargs={"page_id": self.article.pk, "video_job_id": job_id},
        )
        resp = self.client.get(url, **auth)
        self.assertEqual(resp.status_code, 404)

    def test_ready_job_exposes_download_url_and_streams(self):
        job_id, auth = self._start()
        job = VideoJob.objects.get(public_id=job_id)
        from django.core.files.base import ContentFile

        job.mp4.save("v.mp4", ContentFile(b"MP4BYTES"), save=False)
        job.state = VideoJobState.READY
        job.progress = 100.0
        job.save()

        status_url = reverse(
            "wagtailapi_v3:pages_video_status",
            kwargs={"page_id": self.article.pk, "video_job_id": job_id},
        )
        data = self.client.get(status_url, **auth).json()
        self.assertEqual(data["status"], "ready")
        self.assertTrue(data["downloadUrl"].endswith("/download/"))

        dl = self.client.get(data["downloadUrl"], **auth)
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl["Content-Type"], "video/mp4")
        self.assertEqual(b"".join(dl.streaming_content), b"MP4BYTES")
