from django.test import TestCase
from wagtail.rich_text import RichText

from bakerydemo.blog.models import BlogPage
from bakerydemo.videos.narration import body_paragraphs, build_narration


class NarrationTests(TestCase):
    def _page(self, *, title="Tracking Wild Yeast", introduction="An intro.", body=None):
        return BlogPage(title=title, introduction=introduction, body=body or [])

    def test_builds_from_title_intro_and_first_three_body_paragraphs(self):
        page = self._page(
            body=[
                ("paragraph_block", RichText("<p>One.</p><p>Two.</p><p>Three.</p><p>Four.</p>")),
            ]
        )
        script = build_narration(page)
        self.assertEqual(
            script,
            "Tracking Wild Yeast\n\nAn intro.\n\nOne.\n\nTwo.\n\nThree.",
        )
        self.assertNotIn("Four.", script)

    def test_paragraphs_split_across_blocks_and_types(self):
        page = self._page(
            introduction="",
            body=[
                ("heading_block", {"heading_text": "A heading", "size": "h2"}),
                ("paragraph_block", RichText("<p>Body para.</p>")),
                ("block_quote", {"text": "A quote.", "attribute_name": "X"}),
            ],
        )
        paras = body_paragraphs(page)
        self.assertEqual(paras, ["A heading", "Body para.", "A quote."])

    def test_image_and_embed_blocks_contribute_no_text(self):
        page = self._page(
            body=[
                ("embed_block", "https://www.youtube.com/watch?v=abc"),
                ("paragraph_block", RichText("<p>Only this.</p>")),
            ]
        )
        self.assertEqual(body_paragraphs(page), ["Only this."])

    def test_entities_are_decoded(self):
        page = self._page(
            introduction="",
            body=[("paragraph_block", RichText("<p>Salt &amp; flour &lt;3</p>"))],
        )
        self.assertEqual(body_paragraphs(page), ["Salt & flour <3"])

    def test_empty_article_yields_empty_script(self):
        page = self._page(title="", introduction="", body=[])
        self.assertEqual(build_narration(page), "")
