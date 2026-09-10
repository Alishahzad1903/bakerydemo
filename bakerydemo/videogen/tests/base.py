import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

User = get_user_model()


class BakeryVideoTestCase(TestCase):
    """Builds a minimal published blog article plus users/tokens.

    Kept intentionally focused (per AGENTS.md) rather than loading the full demo
    fixture. Stored MP4s are written to a throwaway MEDIA_ROOT so tests never
    pollute the project's media directory.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="videogen-test-media-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        root = Page.objects.get(depth=1)
        self.index = BlogIndexPage(
            title="Blog", slug="blog", introduction="Index intro."
        )
        root.add_child(instance=self.index)

        self.article = BlogPage(
            title="Tracking Wild Yeast",
            slug="tracking-wild-yeast",
            introduction="Wild yeast is everywhere. This sentence is ignored.",
            body="[]",
        )
        self.index.add_child(instance=self.article)
        self.article.save_revision().publish()
        self.article.refresh_from_db()

        # A superuser can publish any page.
        self.superuser = User.objects.create_superuser("boss", "boss@example.com", "pw")
        _, self.super_token = APIToken.create_token(user=self.superuser, name="super")

        # A plain, active user with no page permissions cannot publish.
        self.plain = User.objects.create_user("nobody", "nobody@example.com", "pw")
        _, self.plain_token = APIToken.create_token(user=self.plain, name="plain")

        # An inactive superuser: a valid token but the user is disabled.
        self.inactive = User.objects.create_superuser(
            "ghost", "ghost@example.com", "pw"
        )
        self.inactive.is_active = False
        self.inactive.save(update_fields=["is_active"])
        _, self.inactive_token = APIToken.create_token(
            user=self.inactive, name="inactive"
        )

    def auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}
