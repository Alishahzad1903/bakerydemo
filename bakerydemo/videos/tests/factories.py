"""Test helpers: build a published article and issue API tokens."""

from __future__ import annotations

from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

DEFAULT_INTRO = (
    "Yeasts, with their single-celled growth habit, can be contrasted with "
    "molds, which grow hyphae. Fungal species that can take both forms are "
    "called dimorphic fungi."
)


def build_article(
    title: str = "Tracking Wild Yeast",
    *,
    slug: str = "tracking-wild-yeast",
    introduction: str = DEFAULT_INTRO,
    live: bool = True,
) -> BlogPage:
    """Create a live blog article under a fresh blog index below the root."""
    root = Page.get_first_root_node()
    index = root.get_children().type(BlogIndexPage).first()
    if index is None:
        index = BlogIndexPage(title="Blog", slug="blog")
        root.add_child(instance=index)
        index = index.specific

    article = BlogPage(title=title, slug=slug, introduction=introduction, live=live)
    index.add_child(instance=article)
    return BlogPage.objects.get(pk=article.pk)


def make_token(user, name: str = "test token") -> str:
    """Create an API token for ``user`` and return its plaintext value."""
    _instance, plaintext = APIToken.create_token(user=user, name=name)
    return plaintext


def bearer(token: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}
