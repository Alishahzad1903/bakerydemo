"""Shared helpers for building pages, users and tokens in tests."""

from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from wagtail.models import APIToken, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

User = get_user_model()


def get_blog_index() -> BlogIndexPage:
    index = BlogIndexPage.objects.first()
    if index is not None:
        return index
    root = Page.objects.get(depth=1)
    index = BlogIndexPage(title="Blog", slug=f"blog-{uuid.uuid4().hex[:8]}")
    root.add_child(instance=index)
    return index


def create_blog_article(
    *,
    title: str = "Tracking Wild Yeast",
    introduction: str = (
        "Yeasts, with their single-celled growth habit, can be contrasted with "
        "molds, which grow hyphae. Extra sentence that must never be narrated."
    ),
    live: bool = True,
) -> BlogPage:
    index = get_blog_index()
    article = BlogPage(
        title=title,
        slug=f"article-{uuid.uuid4().hex[:8]}",
        introduction=introduction,
        live=live,
    )
    index.add_child(instance=article)
    return article


def make_user(username: str, *, superuser: bool = False, group: str | None = None,
              active: bool = True) -> User:
    user = User.objects.create(
        username=username,
        is_superuser=superuser,
        is_staff=True,
        is_active=active,
    )
    if group:
        user.groups.add(Group.objects.get(name=group))
    return user


def token_for(user: User, *, name: str = "test token", revoked: bool = False) -> str:
    instance, plaintext = APIToken.create_token(user=user, name=name)
    if revoked:
        instance.revoke()
    return plaintext
