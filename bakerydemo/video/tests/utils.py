"""Shared test helpers for the video app."""

from __future__ import annotations

from django.utils.text import slugify
from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

_counter = 0


def create_blog_article(
    title: str = "Tracking Wild Yeast",
    introduction: str = "Wild yeast is everywhere in the air around us. A second sentence.",
    *,
    live: bool = True,
) -> BlogPage:
    """Create a (by default published) blog article under a blog index."""
    global _counter
    _counter += 1
    root = Page.objects.get(depth=1)
    index = BlogIndexPage.objects.first()
    if index is None:
        index = BlogIndexPage(title="Blog", slug="blog")
        root.add_child(instance=index)
    article = BlogPage(
        title=title,
        slug=f"{slugify(title)}-{_counter}",
        introduction=introduction,
        live=live,
    )
    index.add_child(instance=article)
    return article
