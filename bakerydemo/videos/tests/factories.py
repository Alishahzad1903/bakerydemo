"""Small helpers to build test fixtures without the full demo dataset."""

from __future__ import annotations

import shutil
import tempfile
from datetime import date

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils.text import slugify
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage


class TempMediaRootMixin:
    """Route saved media to a throwaway dir so tests never touch project media."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_root = tempfile.mkdtemp(prefix="videos-test-media-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)
        super().tearDownClass()


_counter = {"n": 0}


def _unique(prefix: str) -> str:
    _counter["n"] += 1
    return f"{prefix}-{_counter['n']}"


def create_blog_article(
    *,
    title: str = "Tracking Wild Yeast",
    introduction: str = (
        "Yeasts, with their single-celled growth habit, can be contrasted "
        "with molds, which grow hyphae. Fungal species that can take both "
        "forms are called dimorphic fungi."
    ),
    live: bool = True,
) -> BlogPage:
    """Create (and optionally publish) a BlogPage under a fresh blog index."""
    root = Page.objects.get(depth=1)
    index = BlogIndexPage(title=_unique("Blog"), slug=_unique("blog"), introduction="")
    root.add_child(instance=index)

    article = BlogPage(
        title=title,
        slug=_unique(slugify(title) or "article"),
        introduction=introduction,
        date_published=date(2020, 1, 1),
    )
    index.add_child(instance=article)
    if live:
        article.save_revision().publish()
    else:
        article.live = False
        article.save()
    article.refresh_from_db()
    return article


def create_user_with_token(
    *, username: str, is_superuser: bool = False, is_active: bool = True
):
    """Create a user and an API token, returning ``(user, plaintext_token)``."""
    from wagtail.models import APIToken

    User = get_user_model()
    user = User.objects.create(
        username=username,
        is_active=is_active,
        is_superuser=is_superuser,
        is_staff=True,
    )
    user.set_password("changeme")
    user.save()
    _instance, plaintext = APIToken.create_token(user=user, name=f"{username}-token")
    return user, plaintext
