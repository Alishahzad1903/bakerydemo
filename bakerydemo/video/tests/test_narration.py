from django.test import TestCase

from bakerydemo.video.narration import build_narration, extract_body_paragraphs

from .helpers import create_blog_article


class NarrationTests(TestCase):
    def test_uses_title_introduction_and_first_three_paragraphs(self):
        article = create_blog_article()
        narration = build_narration(article)

        self.assertIn("Tracking Wild Yeast", narration)
        self.assertIn("An introduction to wild yeast.", narration)
        self.assertIn("First paragraph of the body.", narration)
        self.assertIn("Second paragraph of the body.", narration)
        self.assertIn("Third paragraph of the body.", narration)
        # Only the first three body paragraphs are used.
        self.assertNotIn("Fourth paragraph", narration)

    def test_extract_body_paragraphs_limit(self):
        article = create_blog_article()
        self.assertEqual(len(extract_body_paragraphs(article, limit=3)), 3)
        self.assertEqual(len(extract_body_paragraphs(article, limit=2)), 2)

    def test_handles_empty_body(self):
        article = create_blog_article(body_html="")
        narration = build_narration(article)
        self.assertIn("Tracking Wild Yeast", narration)
        self.assertIn("An introduction to wild yeast.", narration)
