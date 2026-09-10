"""Endpoint tests: the two routes are mounted where the task requires, authenticate
like the rest of the v3 API, admit only publish-permitted callers, and return the
exact top-level field names — all with a faked provider (no network, no billing).
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.urls import reverse
from wagtail.models import APIToken, GroupPagePermission, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

from .support import FakeGateway

GATEWAY = "bakerydemo.videos.service._gateway"


class VideoEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = Page.get_first_root_node()
        cls.index = BlogIndexPage(title="Blog", slug="blog-api")
        cls.root.add_child(instance=cls.index)

        cls.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="yeast-api",
            introduction="Yeasts can be contrasted with molds. Second sentence ignored.",
        )
        cls.index.add_child(instance=cls.article)
        cls.article.save_revision().publish()

        cls.draft = BlogPage(
            title="Draft article", slug="draft-api", introduction="Hi."
        )
        cls.index.add_child(instance=cls.draft)
        cls.draft.unpublish()  # ensure not live

        publish_perm = Permission.objects.get(
            content_type__app_label="wagtailcore", codename="publish_page"
        )
        change_perm = Permission.objects.get(
            content_type__app_label="wagtailcore", codename="change_page"
        )

        # Superuser -> can publish anything.
        cls.admin = User.objects.create_superuser("admin_t", "a@x.com", "pw")
        cls.admin_token = APIToken.create_token(user=cls.admin, name="admin")[1]

        # Publisher group -> can publish (like the seeded "moderator").
        publishers = Group.objects.create(name="Publishers")
        GroupPagePermission.objects.create(
            group=publishers, page=cls.root, permission=publish_perm
        )
        cls.publisher = User.objects.create_user("publisher_t", "p@x.com", "pw")
        cls.publisher.groups.add(publishers)
        cls.publisher_token = APIToken.create_token(user=cls.publisher, name="pub")[1]

        # Editor group -> change but NOT publish (like the seeded "editor").
        editors = Group.objects.create(name="EditorsOnly")
        GroupPagePermission.objects.create(
            group=editors, page=cls.root, permission=change_perm
        )
        cls.editor = User.objects.create_user("editor_t", "e@x.com", "pw")
        cls.editor.groups.add(editors)
        cls.editor_token = APIToken.create_token(user=cls.editor, name="ed")[1]

        # Inactive user (like the seeded "inactive"): auth must refuse it.
        cls.inactive = User.objects.create_superuser("inactive_t", "i@x.com", "pw")
        cls.inactive.is_active = False
        cls.inactive.save()
        cls.inactive_token = APIToken.create_token(user=cls.inactive, name="inact")[1]

        # Revoked token (like the seeded "german").
        token_obj, cls.revoked_token = APIToken.create_token(user=cls.admin, name="rev")
        token_obj.revoke()

    def _start_url(self, page_id=None):
        return reverse(
            "wagtailapi_v3:pages_video_start",
            kwargs={"page_id": page_id or self.article.pk},
        )

    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _post(self, token, page_id=None):
        return self.client.post(
            self._start_url(page_id),
            content_type="application/json",
            **self._auth(token),
        )

    # --- permission matrix ---------------------------------------------------

    def test_superuser_can_start(self):
        with mock.patch(GATEWAY, return_value=FakeGateway()):
            resp = self._post(self.admin_token)
        self.assertEqual(resp.status_code, 202)
        self.assertIn("videoJobId", resp.json())

    def test_publisher_can_start(self):
        with mock.patch(GATEWAY, return_value=FakeGateway()):
            resp = self._post(self.publisher_token)
        self.assertEqual(resp.status_code, 202)

    def test_editor_without_publish_is_forbidden(self):
        with mock.patch(GATEWAY, return_value=FakeGateway()) as gw:
            resp = self._post(self.editor_token)
        self.assertEqual(resp.status_code, 403)
        gw.assert_not_called()  # provider never reached

    def test_inactive_user_is_unauthorized(self):
        resp = self._post(self.inactive_token)
        self.assertEqual(resp.status_code, 401)

    def test_revoked_token_is_unauthorized(self):
        resp = self._post(self.revoked_token)
        self.assertEqual(resp.status_code, 401)

    def test_missing_token_is_unauthorized(self):
        resp = self.client.post(self._start_url(), content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    # --- capability guards ---------------------------------------------------

    def test_non_blog_page_is_not_found(self):
        with mock.patch(GATEWAY, return_value=FakeGateway()):
            resp = self._post(self.admin_token, page_id=self.index.pk)
        self.assertEqual(resp.status_code, 404)

    def test_unpublished_article_is_rejected(self):
        with mock.patch(GATEWAY, return_value=FakeGateway()):
            resp = self._post(self.admin_token, page_id=self.draft.pk)
        self.assertEqual(resp.status_code, 422)

    # --- flow + response shape ----------------------------------------------

    def test_full_flow_and_response_fields(self):
        gateway = FakeGateway()
        with mock.patch(GATEWAY, return_value=gateway):
            start = self._post(self.admin_token)
            self.assertEqual(start.status_code, 202)
            job_id = start.json()["videoJobId"]

            status_url = reverse(
                "wagtailapi_v3:pages_video_status",
                kwargs={"page_id": self.article.pk, "video_job_id": job_id},
            )

            first = self.client.get(status_url, **self._auth(self.admin_token))
            self.assertEqual(first.status_code, 200)
            body = first.json()
            # Exactly the required top-level fields.
            self.assertEqual(
                set(body), {"status", "progressPercentage", "downloadUrl", "error"}
            )
            self.assertEqual(body["status"], "processing")
            self.assertIsNone(body["downloadUrl"])

            # Advance the export to completion.
            second = self.client.get(status_url, **self._auth(self.admin_token)).json()
            self.assertEqual(second["status"], "succeeded")
            self.assertEqual(second["progressPercentage"], 100.0)
            self.assertEqual(second["downloadUrl"], "https://signed.example/video.mp4")
            self.assertIsNone(second["error"])

        # One billable production, one export, for the whole flow.
        self.assertEqual(gateway.calls["start_workflow"], 1)
        self.assertEqual(gateway.calls["start_export"], 1)

    def test_repeat_start_is_idempotent(self):
        gateway = FakeGateway()
        with mock.patch(GATEWAY, return_value=gateway):
            first = self._post(self.admin_token)
            second = self._post(self.admin_token)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)  # already exists
        self.assertEqual(first.json()["videoJobId"], second.json()["videoJobId"])
        self.assertEqual(gateway.calls["start_workflow"], 1)  # not billed twice

    def test_status_requires_publish_permission(self):
        gateway = FakeGateway()
        with mock.patch(GATEWAY, return_value=gateway):
            job_id = self._post(self.admin_token).json()["videoJobId"]
        status_url = reverse(
            "wagtailapi_v3:pages_video_status",
            kwargs={"page_id": self.article.pk, "video_job_id": job_id},
        )
        resp = self.client.get(status_url, **self._auth(self.editor_token))
        self.assertEqual(resp.status_code, 403)
