"""Shared helpers for the videogen test suite."""

from __future__ import annotations

from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage


def get_blog_index() -> BlogIndexPage:
    index = BlogIndexPage.objects.first()
    if index is None:
        root = Page.objects.get(depth=1)
        index = BlogIndexPage(title="Blog", slug="blog", introduction="")
        root.add_child(instance=index)
    return index


def create_blog_article(
    *,
    title="Tracking Wild Yeast",
    slug="tracking-wild-yeast",
    introduction="Yeasts grow as single cells. They differ from molds.",
    live=True,
) -> BlogPage:
    index = get_blog_index()
    article = BlogPage(
        title=title,
        slug=slug,
        introduction=introduction,
        live=live,
    )
    index.add_child(instance=article)
    return article


def bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def make_token(user, name="test-token") -> str:
    _, plaintext = APIToken.create_token(user=user, name=name)
    return plaintext
