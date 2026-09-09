from wagtail.models import Page

from bakerydemo.blog.models import BlogIndexPage, BlogPage

_DEFAULT_BODY = (
    "<p>First paragraph of the body.</p>"
    "<p>Second paragraph of the body.</p>"
    "<p>Third paragraph of the body.</p>"
    "<p>Fourth paragraph should be ignored.</p>"
)


def create_blog_article(
    title="Tracking Wild Yeast",
    introduction="An introduction to wild yeast.",
    body_html=_DEFAULT_BODY,
    live=True,
):
    """Create a published BlogPage under a fresh BlogIndexPage for tests."""
    root = Page.objects.filter(depth=1).first()
    unique = Page.objects.count()
    index = BlogIndexPage(title=f"Blog {unique}", slug=f"blog-{unique}")
    root.add_child(instance=index)
    article = BlogPage(
        title=title,
        slug=f"article-{unique}",
        introduction=introduction,
        body=[("paragraph_block", body_html)],
        live=live,
    )
    index.add_child(instance=article)
    return article
