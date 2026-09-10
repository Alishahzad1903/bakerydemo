from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from bakerydemo.videogen.models import ArticleVideo

from .utils import bearer, create_blog_article, make_token

User = get_user_model()


@override_settings(VIDEOGEN_API_KEY="sk_test_key")
class PageVideoAPITests(TestCase):
    def setUp(self):
        self.article = create_blog_article()
        self.create_url = reverse(
            "wagtailapi_v3:page_video_create", kwargs={"page_id": self.article.id}
        )

        self.publisher = User.objects.create_superuser(
            "publisher", "publisher@example.com", "pw"
        )
        self.publisher_token = make_token(self.publisher)

        self.non_publisher = User.objects.create_user(
            "viewer", "viewer@example.com", "pw"
        )
        self.non_publisher_token = make_token(self.non_publisher)

        # Avoid starting a real background production during API tests.
        patcher = mock.patch("bakerydemo.videogen.api.services.start_video_production")
        self.start_mock = patcher.start()
        self.addCleanup(patcher.stop)

    # -- Authentication / authorization -----------------------------------

    def test_post_without_token_is_401(self):
        response = self.client.post(self.create_url)
        self.assertEqual(response.status_code, 401)
        self.start_mock.assert_not_called()

    def test_post_with_revoked_token_is_401(self):
        from wagtail.models import APIToken

        token_obj, plaintext = APIToken.create_token(
            user=self.publisher, name="revoked"
        )
        token_obj.revoke()
        response = self.client.post(self.create_url, **bearer(plaintext))
        self.assertEqual(response.status_code, 401)

    def test_post_as_non_publisher_is_403(self):
        response = self.client.post(self.create_url, **bearer(self.non_publisher_token))
        self.assertEqual(response.status_code, 403)
        self.start_mock.assert_not_called()

    def test_post_as_publisher_starts_production(self):
        response = self.client.post(self.create_url, **bearer(self.publisher_token))
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("videoJobId", body)
        self.assertEqual(body["status"], ArticleVideo.Status.PENDING)
        self.assertEqual(ArticleVideo.objects.count(), 1)
        self.start_mock.assert_called_once()

    # -- Target validation -------------------------------------------------

    def test_post_on_non_blog_page_is_400(self):
        # The blog *index* is a publishable page, but not a blog article, so a
        # permitted caller gets past the permission gate and hits the 400.
        from .utils import get_blog_index

        index = get_blog_index()
        url = reverse("wagtailapi_v3:page_video_create", kwargs={"page_id": index.id})
        response = self.client.post(url, **bearer(self.publisher_token))
        self.assertEqual(response.status_code, 400)

    def test_post_on_unpublished_article_is_400(self):
        draft = create_blog_article(
            title="Draft", slug="draft-article", live=False
        )
        url = reverse(
            "wagtailapi_v3:page_video_create", kwargs={"page_id": draft.id}
        )
        response = self.client.post(url, **bearer(self.publisher_token))
        self.assertEqual(response.status_code, 400)

    def test_post_on_missing_page_is_404(self):
        url = reverse("wagtailapi_v3:page_video_create", kwargs={"page_id": 999999})
        response = self.client.post(url, **bearer(self.publisher_token))
        self.assertEqual(response.status_code, 404)

    # -- Idempotency / no double billing -----------------------------------

    def test_requesting_twice_returns_same_job_and_starts_once(self):
        first = self.client.post(self.create_url, **bearer(self.publisher_token))
        second = self.client.post(self.create_url, **bearer(self.publisher_token))
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(ArticleVideo.objects.count(), 1)
        self.start_mock.assert_called_once()

    @override_settings(VIDEOGEN_API_KEY="")
    def test_post_without_configuration_is_503(self):
        response = self.client.post(self.create_url, **bearer(self.publisher_token))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(ArticleVideo.objects.count(), 0)

    # -- Status endpoint ---------------------------------------------------

    def _detail_url(self, video):
        return reverse(
            "wagtailapi_v3:page_video_detail",
            kwargs={"page_id": self.article.id, "video_job_id": str(video.id)},
        )

    def test_status_processing_hides_download_and_error(self):
        video = ArticleVideo.objects.create(
            page=self.article,
            script="x",
            status=ArticleVideo.Status.PROCESSING,
            progress_percentage=42,
        )
        response = self.client.get(
            self._detail_url(video), **bearer(self.publisher_token)
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progressPercentage"], 42)
        self.assertIsNone(body["downloadUrl"])
        self.assertIsNone(body["error"])

    def test_status_ready_exposes_download_url(self):
        video = ArticleVideo.objects.create(
            page=self.article,
            script="x",
            status=ArticleVideo.Status.READY,
            progress_percentage=100,
            project_id="p_1",
            export_id="ex_1",
            download_url="https://dl/clip.mp4",
        )
        response = self.client.get(
            self._detail_url(video), **bearer(self.publisher_token)
        )
        body = response.json()
        self.assertEqual(body["status"], "ready")
        self.assertIn("/download/", body["downloadUrl"])

    def test_status_failed_exposes_error(self):
        video = ArticleVideo.objects.create(
            page=self.article,
            script="x",
            status=ArticleVideo.Status.FAILED,
            error="render crashed",
        )
        response = self.client.get(
            self._detail_url(video), **bearer(self.publisher_token)
        )
        body = response.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], "render crashed")
        self.assertIsNone(body["downloadUrl"])

    def test_status_requires_publish_permission(self):
        video = ArticleVideo.objects.create(page=self.article, script="x")
        response = self.client.get(
            self._detail_url(video), **bearer(self.non_publisher_token)
        )
        self.assertEqual(response.status_code, 403)

    def test_status_job_must_belong_to_page(self):
        other = create_blog_article(title="Other", slug="other")
        video = ArticleVideo.objects.create(page=other, script="x")
        # Ask for `video` under `self.article` -> mismatch -> 404.
        url = reverse(
            "wagtailapi_v3:page_video_detail",
            kwargs={"page_id": self.article.id, "video_job_id": str(video.id)},
        )
        response = self.client.get(url, **bearer(self.publisher_token))
        self.assertEqual(response.status_code, 404)

    # -- Download endpoint -------------------------------------------------

    def _download_url(self, video):
        return reverse(
            "wagtailapi_v3:page_video_download",
            kwargs={"page_id": self.article.id, "video_job_id": str(video.id)},
        )

    def test_download_redirects_to_fresh_url(self):
        video = ArticleVideo.objects.create(
            page=self.article,
            script="x",
            status=ArticleVideo.Status.READY,
            project_id="p_1",
            export_id="ex_1",
            download_url="https://stale/clip.mp4",
        )
        with mock.patch(
            "bakerydemo.videogen.api.services.get_fresh_download_url",
            return_value="https://fresh/clip.mp4",
        ):
            response = self.client.get(
                self._download_url(video), **bearer(self.publisher_token)
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://fresh/clip.mp4")

    def test_download_falls_back_to_cached_url_on_provider_error(self):
        from bakerydemo.videogen.exceptions import VideoGenServerError

        video = ArticleVideo.objects.create(
            page=self.article,
            script="x",
            status=ArticleVideo.Status.READY,
            project_id="p_1",
            export_id="ex_1",
            download_url="https://cached/clip.mp4",
        )
        with mock.patch(
            "bakerydemo.videogen.api.services.get_fresh_download_url",
            side_effect=VideoGenServerError("down", status=500),
        ):
            response = self.client.get(
                self._download_url(video), **bearer(self.publisher_token)
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://cached/clip.mp4")

    def test_download_before_ready_is_409(self):
        video = ArticleVideo.objects.create(
            page=self.article,
            script="x",
            status=ArticleVideo.Status.PROCESSING,
        )
        response = self.client.get(
            self._download_url(video), **bearer(self.publisher_token)
        )
        self.assertEqual(response.status_code, 409)
