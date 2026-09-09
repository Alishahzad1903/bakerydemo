from django.contrib.auth import get_user_model
from wagtail.models import APIToken, Page
from wagtail.rich_text import RichText

from bakerydemo.blog.models import BlogIndexPage, BlogPage


def make_blog_article(title="Wild Yeast", slug="wild-yeast", live=True):
    root = Page.objects.filter(depth=1).first()
    index = root.add_child(
        instance=BlogIndexPage(title="Test Blog", slug=f"test-blog-{slug}")
    )
    article = index.add_child(
        instance=BlogPage(
            title=title,
            slug=slug,
            introduction="A short introduction to wild yeast.",
            live=live,
            body=[
                ("paragraph_block", RichText("<p>First paragraph.</p><p>Second.</p>")),
                ("paragraph_block", RichText("<p>Third paragraph.</p>")),
            ],
        )
    )
    return article


def make_user(username, *, superuser=False):
    User = get_user_model()
    return User.objects.create_user(
        username=username,
        password="x",
        is_superuser=superuser,
        is_staff=superuser,
    )


def make_token(user, name="test"):
    _, plaintext = APIToken.create_token(user=user, name=name)
    return plaintext
