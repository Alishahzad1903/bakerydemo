"""Helpers to build a page tree and a permission/token matrix for tests.

Mirrors the shape of the seeded demo content: superusers, a "moderators" group
that may publish (via a cascading root-page permission), an "editors" group that
may not, plus inactive-user and revoked-token cases.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from wagtail.models import APIToken, GroupPagePermission, Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

User = get_user_model()

TRACKING_WILD_YEAST_INTRO = (
    "Yeasts, with their single-celled growth habit, can be contrasted with "
    "molds, which grow hyphae. Fungal species that can take both forms are "
    "called dimorphic fungi."
)


def build_blog_tree():
    """Create a published blog article (and a draft one) under the root page."""
    root = Page.get_first_root_node()
    index = BlogIndexPage(title="Blog", slug="blog-test")
    root.add_child(instance=index)

    article = BlogPage(
        title="Tracking Wild Yeast",
        slug="wild-yeast-test",
        introduction=TRACKING_WILD_YEAST_INTRO,
    )
    index.add_child(instance=article)
    article.save_revision().publish()
    article.refresh_from_db()

    draft = BlogPage(
        title="Unpublished Draft",
        slug="draft-test",
        introduction="Draft intro sentence. More.",
        live=False,
    )
    index.add_child(instance=draft)

    return {"root": root, "index": index, "article": article, "draft": draft}


def _publish_permission():
    return Permission.objects.get(
        content_type__app_label="wagtailcore", codename="publish_page"
    )


def make_groups(root):
    """Return (moderators, editors): moderators may publish, editors may not."""
    moderators, _ = Group.objects.get_or_create(name="Moderators-test")
    editors, _ = Group.objects.get_or_create(name="Editors-test")
    GroupPagePermission.objects.create(
        group=moderators, page=root, permission=_publish_permission()
    )
    GroupPagePermission.objects.create(
        group=editors,
        page=root,
        permission=Permission.objects.get(
            content_type__app_label="wagtailcore", codename="change_page"
        ),
    )
    return moderators, editors


def make_user_with_token(
    username, *, is_superuser=False, is_active=True, group=None, revoked=False
):
    """Create a user and an API token, returning (user, plaintext_token)."""
    user = User.objects.create(
        username=username,
        is_superuser=is_superuser,
        is_staff=is_superuser,
        is_active=is_active,
    )
    if group is not None:
        user.groups.add(group)
    token, plaintext = APIToken.create_token(user=user, name=f"{username} token")
    if revoked:
        token.revoke()
    return user, plaintext
