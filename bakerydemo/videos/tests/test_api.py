from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos.models import ArticleVideo

START_PATCH = "bakerydemo.videos.api.start_production"


class VideoApiTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        root = Page.get_first_root_node()
        # A BlogIndexPage is a Page but not a BlogPage — used below to check
        # that a non-article page is rejected.
        cls.index = BlogIndexPage(title="Blog", slug="blog", introduction="Blog")
        root.add_child(instance=cls.index)

        cls.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="tracking-wild-yeast",
            introduction="Yeasts are fascinating. They are single-celled organisms.",
        )
        cls.index.add_child(instance=cls.article)
        cls.article.save_revision().publish()
        cls.article.refresh_from_db()

        cls.draft = BlogPage(
            title="Draft Article",
            slug="draft-article",
            introduction="Not yet published.",
            live=False,
        )
        cls.index.add_child(instance=cls.draft)

        # A caller who may publish (superuser), and one who may not.
        cls.publisher = User.objects.create_superuser("pub", "pub@example.com", "pw")
        cls.non_publisher = User.objects.create_user("nop", "nop@example.com", "pw")

        _, cls.pub_token = APIToken.create_token(user=cls.publisher, name="pub")
        _, cls.nop_token = APIToken.create_token(user=cls.non_publisher, name="nop")

    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def start_url(self, page_id):
        return f"/api/v3-preview/pages/{page_id}/video/"

    def status_url(self, page_id, job_id):
        return f"/api/v3-preview/pages/{page_id}/video/{job_id}/"


class StartVideoTests(VideoApiTestCase):
    def test_publisher_can_start_and_gets_job_id(self):
        with mock.patch(START_PATCH) as start:
            resp = self.client.post(
                self.start_url(self.article.pk), **self._auth(self.pub_token)
            )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("videoJobId", body)
        self.assertTrue(ArticleVideo.objects.filter(job_id=body["videoJobId"]).exists())
        start.assert_called_once()

    def test_asking_twice_reuses_the_same_job(self):
        with mock.patch(START_PATCH) as start:
            first = self.client.post(self.start_url(self.article.pk), **self._auth(self.pub_token))
            second = self.client.post(self.start_url(self.article.pk), **self._auth(self.pub_token))

        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(ArticleVideo.objects.filter(page=self.article).count(), 1)
        # Production kicked off once, so no second video is produced or billed.
        start.assert_called_once()

    def test_failed_job_does_not_block_a_fresh_attempt(self):
        failed = ArticleVideo.objects.create(
            page=self.article, script="x", status=ArticleVideo.Status.FAILED, error="boom"
        )
        with mock.patch(START_PATCH):
            resp = self.client.post(self.start_url(self.article.pk), **self._auth(self.pub_token))
        new_id = resp.json()["videoJobId"]
        self.assertNotEqual(new_id, str(failed.job_id))
        self.assertEqual(ArticleVideo.objects.filter(page=self.article).count(), 2)

    def test_non_publisher_is_forbidden(self):
        with mock.patch(START_PATCH) as start:
            resp = self.client.post(self.start_url(self.article.pk), **self._auth(self.nop_token))
        self.assertEqual(resp.status_code, 403)
        start.assert_not_called()

    def test_missing_token_is_unauthorized(self):
        resp = self.client.post(self.start_url(self.article.pk))
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_is_unauthorized(self):
        resp = self.client.post(
            self.start_url(self.article.pk), **self._auth("wagtail_not_a_real_token")
        )
        self.assertEqual(resp.status_code, 401)

    def test_unpublished_article_is_conflict(self):
        with mock.patch(START_PATCH):
            resp = self.client.post(self.start_url(self.draft.pk), **self._auth(self.pub_token))
        self.assertEqual(resp.status_code, 409)

    def test_non_blog_page_is_not_found(self):
        with mock.patch(START_PATCH):
            resp = self.client.post(self.start_url(self.index.pk), **self._auth(self.pub_token))
        self.assertEqual(resp.status_code, 404)

    def test_unknown_page_is_not_found(self):
        with mock.patch(START_PATCH):
            resp = self.client.post(self.start_url(999999), **self._auth(self.pub_token))
        self.assertEqual(resp.status_code, 404)


class VideoStatusTests(VideoApiTestCase):
    def test_processing_status_shape(self):
        job = ArticleVideo.objects.create(
            page=self.article, script="x", status=ArticleVideo.Status.PROCESSING, progress_percentage=12.0
        )
        resp = self.client.get(
            self.status_url(self.article.pk, job.job_id), **self._auth(self.pub_token)
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progressPercentage"], 12.0)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])

    def test_failed_status_carries_error(self):
        job = ArticleVideo.objects.create(
            page=self.article, script="x", status=ArticleVideo.Status.FAILED, error="it broke"
        )
        resp = self.client.get(
            self.status_url(self.article.pk, job.job_id), **self._auth(self.pub_token)
        )
        body = resp.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "it broke")
        self.assertIsNone(body["downloadUrl"])

    def test_ready_status_refreshes_download_url(self):
        job = ArticleVideo.objects.create(
            page=self.article,
            script="x",
            status=ArticleVideo.Status.READY,
            progress_percentage=100.0,
            project_id="vg_proj_x",
            export_id="vg_expo_x",
            download_url="https://cdn/old.mp4",
        )
        from bakerydemo.videos.service import JobSnapshot

        fresh = JobSnapshot(status="succeeded", progress_percentage=100.0, download_url="https://cdn/fresh.mp4")
        with mock.patch(
            "bakerydemo.videos.api.VideoGenService"
        ) as svc_cls:
            svc_cls.return_value.get_project_export.return_value = fresh
            resp = self.client.get(
                self.status_url(self.article.pk, job.job_id), **self._auth(self.pub_token)
            )
        self.assertEqual(resp.json()["downloadUrl"], "https://cdn/fresh.mp4")

    def test_non_publisher_cannot_read_status(self):
        job = ArticleVideo.objects.create(page=self.article, script="x")
        resp = self.client.get(
            self.status_url(self.article.pk, job.job_id), **self._auth(self.nop_token)
        )
        self.assertEqual(resp.status_code, 403)

    def test_unknown_job_is_not_found(self):
        resp = self.client.get(
            self.status_url(self.article.pk, "123e4567-e89b-12d3-a456-426614174000"),
            **self._auth(self.pub_token),
        )
        self.assertEqual(resp.status_code, 404)

    def test_malformed_job_id_is_not_found(self):
        resp = self.client.get(
            self.status_url(self.article.pk, "not-a-uuid"), **self._auth(self.pub_token)
        )
        self.assertEqual(resp.status_code, 404)
