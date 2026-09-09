from types import SimpleNamespace

from django.test import SimpleTestCase

from bakerydemo.videos.narration import build_narration, extract_body_paragraphs


class FakeBlock:
    def __init__(self, block_type, source):
        self.block_type = block_type
        self.value = SimpleNamespace(source=source)


def make_page(title="", introduction="", blocks=None):
    return SimpleNamespace(title=title, introduction=introduction, body=blocks or [])


class NarrationTests(SimpleTestCase):
    def test_uses_only_title_intro_and_first_three_paragraphs(self):
        blocks = [
            FakeBlock("paragraph_block", "<p>One.</p><p>Two.</p>"),
            FakeBlock("heading_block", "ignored heading"),  # not a paragraph_block
            FakeBlock("paragraph_block", "<p>Three.</p><p>Four.</p>"),
        ]
        page = make_page(title="Title", introduction="Intro.", blocks=blocks)
        script = build_narration(page)
        self.assertEqual(script, "Title\n\nIntro.\n\nOne.\n\nTwo.\n\nThree.")
        self.assertNotIn("Four.", script)
        self.assertNotIn("ignored heading", script)

    def test_headings_inside_rich_text_do_not_glue_to_paragraph(self):
        blocks = [
            FakeBlock("paragraph_block", "<p>Lead.</p><h2>Section</h2><p>Body.</p>")
        ]
        paras = extract_body_paragraphs(make_page(blocks=blocks))
        self.assertEqual(paras, ["Lead.", "Section", "Body."])

    def test_strips_tags_and_normalises_whitespace_and_entities(self):
        blocks = [FakeBlock("paragraph_block", "<p>Salt &amp;   pepper\n<b>bold</b></p>")]
        paras = extract_body_paragraphs(make_page(blocks=blocks))
        self.assertEqual(paras, ["Salt & pepper bold"])

    def test_empty_body_falls_back_to_title_and_intro(self):
        self.assertEqual(build_narration(make_page(title="T", introduction="I")), "T\n\nI")

    def test_only_title(self):
        self.assertEqual(build_narration(make_page(title="Just a title")), "Just a title")
