"""Unit tests for narration building, the provider gateway (error boundary and
outgoing request shape), and the state machine — all without a network."""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase
from videogen import VideogenClient
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage
from bakerydemo.videos import service
from bakerydemo.videos.exceptions import (
    VideoGenAPIError,
    VideoGenResponseError,
    VideoGenUnavailableError,
)
from bakerydemo.videos.models import VideoJob
from bakerydemo.videos.service import VideoGenGateway, build_script

from .support import BoomTransport, FakeGateway, StubTransport, json_response


class BuildScriptTests(SimpleTestCase):
    def test_title_plus_first_intro_sentence(self):
        script = build_script(
            "Tracking Wild Yeast",
            "Yeasts can be contrasted with molds. A second sentence is ignored.",
            max_words=30,
        )
        self.assertEqual(
            script, "Tracking Wild Yeast. Yeasts can be contrasted with molds."
        )

    def test_word_cap_is_enforced(self):
        script = build_script("Title", "one two three four five six", max_words=4)
        self.assertEqual(len(script.split()), 4)

    def test_title_only_when_no_introduction(self):
        self.assertEqual(
            build_script("Just A Title", "", max_words=30), "Just A Title."
        )

    def test_uses_only_supplied_text(self):
        # Nothing is fetched or invented: output is a substring-ish of the inputs.
        script = build_script("T", "Sentence one. Sentence two.", max_words=30)
        self.assertIn("Sentence one", script)
        self.assertNotIn("Sentence two", script)


def _stub_client(*responses) -> tuple[VideogenClient, StubTransport]:
    transport = StubTransport(*responses)
    client = VideogenClient(bearer_auth="test-key", custom_http_client=transport)
    return client, transport


class GatewayRequestShapeTests(SimpleTestCase):
    """The gateway must always ask for the cheap, mandated shape."""

    def test_start_workflow_sends_stock_voice_16_9(self):
        client, transport = _stub_client(
            json_response(
                200,
                {
                    "workflowRunId": "vg_work_1",
                    "projectId": "vg_proj_1",
                    "projectUrl": "https://app.videogen.io/p/1",
                    "remixActionIds": [],
                },
            )
        )
        VideoGenGateway(client).start_workflow("Hello world.")

        body = transport.last_request.body.value
        self.assertEqual(body["script"], "Hello world.")
        self.assertEqual(body["visualStyle"]["type"], "STOCK")  # stock footage only
        self.assertEqual(body["aspectRatio"], {"width": 16, "height": 9})  # 16:9
        self.assertNotIn("actorEntityId", body)  # voice only, no avatar
        self.assertNotIn("remixActions", body)  # no image-to-video etc.

    def test_export_requests_720p_standard_tier(self):
        client, transport = _stub_client(json_response(200, {"exportId": "vg_expo_1"}))
        VideoGenGateway(client).start_export("vg_proj_1")

        body = transport.last_request.body.value
        # STANDARD is the 720p tier (HIGH was observed to render 1080p);
        # never ULTRA_HIGH/4K.
        self.assertEqual(body["quality"], "STANDARD")


class GatewayErrorBoundaryTests(SimpleTestCase):
    """Every provider failure surfaces as a typed VideoGenError."""

    def test_provider_error_becomes_api_error(self):
        client, _ = _stub_client(
            json_response(
                429, {"message": "out of credits", "code": "insufficient_credits"}
            )
        )
        with self.assertRaises(VideoGenAPIError) as ctx:
            VideoGenGateway(client).get_workflow_run("vg_work_1")
        self.assertEqual(ctx.exception.status_code, 429)

    def test_undecodable_success_body_becomes_response_error(self):
        # 200 but a required field (workflowRunId) is missing -> decode failure.
        client, _ = _stub_client(json_response(200, {"projectId": "vg_proj_1"}))
        with self.assertRaises(VideoGenResponseError):
            VideoGenGateway(client).start_workflow("Hello.")

    def test_transport_failure_becomes_unavailable(self):
        client = VideogenClient(
            bearer_auth="test-key", custom_http_client=BoomTransport()
        )
        with self.assertRaises(VideoGenUnavailableError):
            VideoGenGateway(client).get_workflow_run("vg_work_1")


class StateMachineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = Page.get_first_root_node()
        cls.index = BlogIndexPage(title="Blog", slug="blog-sm")
        cls.root.add_child(instance=cls.index)
        cls.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="yeast-sm",
            introduction="Yeasts can be contrasted with molds. Second sentence.",
        )
        cls.index.add_child(instance=cls.article)
        cls.article.save_revision().publish()

    def _drive_to_terminal(self, gateway: FakeGateway) -> VideoJob:
        job, created = service.start_video(self.article, gateway=gateway)
        self.assertTrue(created)
        for _ in range(10):
            job = service.advance(job, gateway=gateway)
            if job.is_terminal:
                break
        return job

    def test_happy_path_reaches_ready_with_one_build_and_one_export(self):
        gateway = FakeGateway(
            workflow_states=["running", "succeeded"],
            export_states=["running", "succeeded"],
        )
        job = self._drive_to_terminal(gateway)

        self.assertEqual(job.status, VideoJob.Status.READY)
        self.assertEqual(job.public_status, "succeeded")
        self.assertEqual(job.progress_percentage, 100.0)
        self.assertEqual(job.download_url, "https://signed.example/video.mp4")
        # Exactly one billable production and one export.
        self.assertEqual(gateway.calls["start_workflow"], 1)
        self.assertEqual(gateway.calls["start_export"], 1)
        # Narration was the title + first sentence, verbatim.
        self.assertEqual(
            gateway.last_script,
            "Tracking Wild Yeast. Yeasts can be contrasted with molds.",
        )

    def test_idempotent_second_start_does_not_produce_a_second_video(self):
        gateway = FakeGateway()
        job1, created1 = service.start_video(self.article, gateway=gateway)
        job2, created2 = service.start_video(self.article, gateway=gateway)

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(job1.pk, job2.pk)
        self.assertEqual(VideoJob.objects.filter(page=self.article).count(), 1)
        self.assertEqual(gateway.calls["start_workflow"], 1)  # not billed twice

    def test_repeated_poll_at_build_success_triggers_only_one_export(self):
        gateway = FakeGateway(workflow_states=["succeeded"], export_states=["running"])
        job, _ = service.start_video(self.article, gateway=gateway)
        # Poll several times while the export runs.
        for _ in range(4):
            job = service.advance(job, gateway=gateway)
        self.assertEqual(gateway.calls["start_export"], 1)  # single export only

    def test_workflow_failure_marks_job_failed(self):
        gateway = FakeGateway(
            workflow_states=["failed"], workflow_error="model unavailable"
        )
        job = self._drive_to_terminal(gateway)
        self.assertEqual(job.status, VideoJob.Status.FAILED)
        self.assertEqual(job.public_status, "failed")
        self.assertIn("model unavailable", job.error_message)
        self.assertEqual(
            gateway.calls["start_export"], 0
        )  # never exported a failed build

    def test_export_failure_marks_job_failed(self):
        gateway = FakeGateway(export_states=["failed"], export_error="render error")
        job = self._drive_to_terminal(gateway)
        self.assertEqual(job.status, VideoJob.Status.FAILED)
        self.assertIn("render error", job.error_message)
