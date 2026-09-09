from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos import service
from bakerydemo.videos.exceptions import VideoGenAPIError
from bakerydemo.videos.models import ArticleVideo, VideoStatus

from ._videogen_stub import (
    StubTransport,
    client_with,
    export_started_body,
    json_response,
    project_export_body,
    start_workflow_body,
    workflow_run_body,
)


def _make_article(title="Tracking Wild Yeast", slug="twy", live=True):
    root = Page.get_first_root_node()
    index = BlogIndexPage.objects.filter(slug="blog").first()
    if index is None:
        index = BlogIndexPage(title="Blog", slug="blog", introduction="")
        root.add_child(instance=index)
    article = BlogPage(
        title=title,
        slug=slug,
        introduction="Yeasts grow as single cells. They are fungi.",
        live=live,
    )
    index.add_child(instance=article)
    if live:
        article.save_revision().publish()
    return article


class RunProductionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.article = _make_article()

    def setUp(self):
        self.video = ArticleVideo.objects.create(page=self.article, script="s")

    @patch("bakerydemo.videos.service._store_mp4")
    @patch(
        "bakerydemo.videos.videogen_service.produce_video",
        return_value="https://cdn/v.mp4",
    )
    def test_success_marks_ready_and_stores_file(self, mock_produce, mock_store):
        def fake_store(video, url):
            video.mp4.save("v.mp4", ContentFile(b"mp4-bytes"), save=True)

        mock_store.side_effect = fake_store

        service.run_production(self.video.pk, "s")

        self.video.refresh_from_db()
        self.assertEqual(self.video.status, VideoStatus.SUCCEEDED)
        self.assertEqual(self.video.progress, 100)
        self.assertTrue(self.video.mp4)
        mock_produce.assert_called_once()
        mock_store.assert_called_once_with(self.video, "https://cdn/v.mp4")

    @patch(
        "bakerydemo.videos.videogen_service.produce_video",
        side_effect=VideoGenAPIError("boom", status_code=400, detail="d"),
    )
    def test_provider_failure_recorded(self, mock_produce):
        service.run_production(self.video.pk, "s")
        self.video.refresh_from_db()
        self.assertEqual(self.video.status, VideoStatus.FAILED)
        self.assertIn("boom", self.video.error)

    @patch("bakerydemo.videos.service._store_mp4")
    @patch("bakerydemo.videos.videogen_service.build_client")
    def test_full_flow_persists_provider_ids(self, mock_build_client, mock_store):
        # Drive the real produce_video against a fake transport (no network),
        # so the hooks that persist provider ids are exercised too.
        mock_build_client.return_value = client_with(
            StubTransport(
                json_response(200, start_workflow_body("vg_work_9", "vg_proj_9")),
                json_response(200, workflow_run_body("succeeded", 100.0)),
                json_response(200, export_started_body("vg_expo_9")),
                json_response(
                    200, project_export_body("succeeded", 100.0, "https://cdn/v.mp4")
                ),
            )
        )
        mock_store.side_effect = lambda video, url: video.mp4.save(
            "v.mp4", ContentFile(b"x"), save=True
        )

        with self.settings(VIDEOGEN_POLL_INTERVAL=0):
            service.run_production(self.video.pk, "s")

        self.video.refresh_from_db()
        self.assertEqual(self.video.status, VideoStatus.SUCCEEDED)
        self.assertEqual(self.video.provider_workflow_run_id, "vg_work_9")
        self.assertEqual(self.video.provider_project_id, "vg_proj_9")
        self.assertEqual(self.video.provider_export_id, "vg_expo_9")


class StartVideoForPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.article = _make_article()

    @patch("bakerydemo.videos.service.enqueue_production")
    def test_idempotent_no_second_production(self, mock_enqueue):
        v1, started1 = service.start_video_for_page(self.article)
        self.assertTrue(started1)

        v2, started2 = service.start_video_for_page(self.article)
        self.assertFalse(started2)
        self.assertEqual(v1.pk, v2.pk)
        # Only the first request scheduled a production.
        mock_enqueue.assert_called_once()
        self.assertEqual(ArticleVideo.objects.count(), 1)

    @patch("bakerydemo.videos.service.enqueue_production")
    def test_records_script(self, mock_enqueue):
        video, _ = service.start_video_for_page(self.article)
        self.assertEqual(
            video.script,
            "Tracking Wild Yeast. Yeasts grow as single cells.",
        )

    @patch("bakerydemo.videos.service.enqueue_production")
    def test_failed_job_restarts(self, mock_enqueue):
        video, _ = service.start_video_for_page(self.article)
        video.mark_failed("earlier failure")

        video2, started = service.start_video_for_page(self.article)
        self.assertTrue(started)
        self.assertEqual(video.pk, video2.pk)
        self.assertEqual(video2.status, VideoStatus.PENDING)
        self.assertEqual(video2.error, "")
        self.assertEqual(mock_enqueue.call_count, 2)
