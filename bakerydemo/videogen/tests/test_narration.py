from types import SimpleNamespace

from django.test import SimpleTestCase

from bakerydemo.videogen.narration import build_narration_script, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_extracts_first_sentence(self):
        self.assertEqual(
            first_sentence("One. Two. Three."),
            "One.",
        )

    def test_handles_question_and_exclamation(self):
        self.assertEqual(first_sentence("Really? Yes indeed."), "Really?")
        self.assertEqual(first_sentence("Wow! More text."), "Wow!")

    def test_collapses_whitespace(self):
        self.assertEqual(first_sentence("  A  sentence\nhere. Next."), "A sentence here.")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")


class BuildNarrationScriptTests(SimpleTestCase):
    def _page(self, title="", introduction=""):
        return SimpleNamespace(title=title, introduction=introduction)

    def test_title_plus_first_sentence(self):
        page = self._page(
            title="Tracking Wild Yeast",
            introduction="Yeasts grow as single cells. They differ from molds.",
        )
        self.assertEqual(
            build_narration_script(page),
            "Tracking Wild Yeast. Yeasts grow as single cells.",
        )

    def test_body_is_never_included(self):
        page = self._page(
            title="Title",
            introduction="Intro sentence one. Intro sentence two.",
        )
        script = build_narration_script(page)
        self.assertNotIn("two", script)

    def test_caps_at_30_words(self):
        intro = "word " * 60
        page = self._page(title="A B C", introduction=intro.strip() + ".")
        script = build_narration_script(page)
        self.assertLessEqual(len(script.split()), 30)

    def test_respects_custom_max_words(self):
        page = self._page(title="One two three", introduction="four five six. seven.")
        self.assertEqual(build_narration_script(page, max_words=4).split().__len__(), 4)

    def test_title_only_when_intro_empty(self):
        page = self._page(title="Just A Title", introduction="")
        self.assertEqual(build_narration_script(page), "Just A Title")

    def test_strips_trailing_title_punctuation(self):
        page = self._page(title="Hello!", introduction="World goes here.")
        self.assertEqual(build_narration_script(page), "Hello. World goes here.")
