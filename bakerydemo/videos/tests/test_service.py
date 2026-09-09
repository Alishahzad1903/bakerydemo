from unittest import mock

import httpx
from django.test import TestCase, override_settings
from wagtail.models import Page
from wagtail.rich_text import RichText

from bakerydemo.blog.models import BlogPage
from bakerydemo.videos import service
from bakerydemo.videos.constants import VideoStatus
from bakerydemo.videos.exceptions import (
    VideoGenApiError,
    VideoGenContentError,
    VideoGenProductionError,
    VideoGenUnreachableError,
    VideoGenUnreadableResponseError,
)
from bakerydemo.videos.models import ArticleVideo

from .stubs import build_client, json_response


def _start_body(**overrides):
    body = {
        "workflowRunId": "vg_work_test",
        "projectId": "vg_proj_test",
        "projectUrl": "https://app.videogen.io/p/vg_proj_test",
        "remixActionIds": [],
    }
    body.update(overrides)
    return body


def _workflow_run_body(status="succeeded", progress=100.0, error=None):
    return {
        "workflowRunId": "vg_work_test",
        "status": status,
        "workflowType": "SCRIPT_TO_VIDEO",
        "progressPercentage": progress,
        "attemptIndex": 0,
        "projectId": "vg_proj_test",
        "projectUrl": "https://app.videogen.io/p/vg_proj_test",
        "error": error,
    }


def _export_start_body():
    return {"exportId": "vg_expo_test"}


def _project_export_body(status="succeeded", download_url="https://signed.example/video.mp4", error=None):
    return {
        "exportId": "vg_expo_test",
        "projectId": "vg_proj_test",
        "status": status,
        "progressPercentage": 100.0,
        "attemptIndex": 0,
        "downloadUrl": download_url,
        "downloadUrlExpiresAt": 1893456000,
        "thumbnailUrl": None,
        "thumbnailUrlExpiresAt": None,
        "exportFileId": "vg_file_test",
        "file": None,
        "error": error,
    }


@override_settings(VIDEOGEN_POLL_INTERVAL=0, VIDEOGEN_MAX_WAIT_SECONDS=30, VIDEOGEN_EXPORT_QUALITY="FULL_HIGH")
class ProduceVideoTests(TestCase):
    def setUp(self):
        root = Page.get_first_root_node()
        self.page = root.add_child(
            instance=BlogPage(
                title="Tracking Wild Yeast",
                slug="tracking-wild-yeast",
                introduction="How we chase wild yeast.",
                body=[("paragraph_block", RichText("<p>First.</p><p>Second.</p>"))],
                live=True,
            )
        )
        self.article_video = ArticleVideo.objects.create(page=self.page)

    def test_success_path_stores_mp4_and_marks_ready(self):
        client, transport = build_client(
            json_response(200, _start_body()),
            json_response(200, _workflow_run_body()),
            json_response(200, _export_start_body()),
            json_response(200, _project_export_body()),
        )
        downloaded = {}

        def downloader(url):
            downloaded["url"] = url
            return b"FAKE-MP4-BYTES"

        service.produce_video(self.article_video, client=client, downloader=downloader)

        self.article_video.refresh_from_db()
        self.assertEqual(self.article_video.status, VideoStatus.READY)
        self.assertEqual(self.article_video.progress_percentage, 100.0)
        self.assertTrue(self.article_video.video_file)
        self.assertEqual(self.article_video.video_file.read(), b"FAKE-MP4-BYTES")
        self.assertEqual(downloaded["url"], "https://signed.example/video.mp4")
        self.assertEqual(self.article_video.videogen_workflow_run_id, "vg_work_test")
        self.assertEqual(self.article_video.videogen_export_id, "vg_expo_test")

    def test_request_shape_is_stock_voiceover_and_capped_quality(self):
        client, transport = build_client(
            json_response(200, _start_body()),
            json_response(200, _workflow_run_body()),
            json_response(200, _export_start_body()),
            json_response(200, _project_export_body()),
        )
        service.produce_video(self.article_video, client=client, downloader=lambda url: b"x")

        script_request = transport.requests[0]
        payload = script_request.body.value
        self.assertEqual(payload["visualStyle"]["type"], "STOCK")
        self.assertNotIn("actorEntityId", payload)  # voiceover only, no avatar
        self.assertEqual(payload["aspectRatio"], {"width": 16, "height": 9})
        self.assertIn("Tracking Wild Yeast", payload["script"])
        self.assertIn("First.", payload["script"])

        export_request = transport.requests[2]
        self.assertEqual(export_request.body.value["quality"], "FULL_HIGH")

    def test_workflow_failure_raises_production_error_with_message(self):
        client, _ = build_client(
            json_response(200, _start_body()),
            json_response(200, _workflow_run_body(status="failed", error={"message": "no stock footage"})),
        )
        with self.assertRaises(VideoGenProductionError) as ctx:
            service.produce_video(self.article_video, client=client, downloader=lambda url: b"x")
        self.assertIn("no stock footage", str(ctx.exception))

    def test_api_error_is_translated_with_status(self):
        client, _ = build_client(
            json_response(402, {"message": "out of credits", "code": "insufficient_credits"}),
        )
        with self.assertRaises(VideoGenApiError) as ctx:
            service.produce_video(self.article_video, client=client, downloader=lambda url: b"x")
        self.assertEqual(ctx.exception.status_code, 402)

    def test_truncated_success_body_is_unreadable(self):
        client, _ = build_client(json_response(200, {}))  # missing required members
        with self.assertRaises(VideoGenUnreadableResponseError):
            service.produce_video(self.article_video, client=client, downloader=lambda url: b"x")

    def test_transport_failure_is_unreachable(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        client, _ = build_client(boom)
        with self.assertRaises(VideoGenUnreachableError):
            service.produce_video(self.article_video, client=client, downloader=lambda url: b"x")

    def test_export_without_download_url_is_unreadable(self):
        client, _ = build_client(
            json_response(200, _start_body()),
            json_response(200, _workflow_run_body()),
            json_response(200, _export_start_body()),
            json_response(200, _project_export_body(download_url=None)),
        )
        with self.assertRaises(VideoGenUnreadableResponseError):
            service.produce_video(self.article_video, client=client, downloader=lambda url: b"x")

    def test_empty_narration_raises_content_error_before_any_call(self):
        client, transport = build_client()  # no responses queued
        with mock.patch(
            "bakerydemo.videos.service.build_narration", return_value=""
        ), self.assertRaises(VideoGenContentError):
            service.produce_video(self.article_video, client=client, downloader=lambda url: b"x")
        self.assertEqual(transport.requests, [])  # nothing sent, nothing billed
