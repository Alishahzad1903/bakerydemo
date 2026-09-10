from types import SimpleNamespace

from django.test import SimpleTestCase

from bakerydemo.videos.narration import build_narration_script, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_extracts_first_sentence(self):
        text = "Yeasts can be contrasted with molds. They grow differently."
        self.assertEqual(
            first_sentence(text), "Yeasts can be contrasted with molds."
        )

    def test_no_terminator_returns_whole_text(self):
        self.assertEqual(first_sentence("a short intro"), "a short intro")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")


class BuildNarrationScriptTests(SimpleTestCase):
    def _page(self, title, introduction=""):
        return SimpleNamespace(title=title, introduction=introduction)

    def test_title_plus_first_intro_sentence(self):
        page = self._page(
            "Tracking Wild Yeast",
            "Yeasts, with their single-celled growth habit, can be contrasted "
            "with molds. Second sentence is ignored.",
        )
        script = build_narration_script(page)
        self.assertEqual(
            script,
            "Tracking Wild Yeast. Yeasts, with their single-celled growth "
            "habit, can be contrasted with molds.",
        )
        self.assertNotIn("Second sentence", script)

    def test_word_cap_enforced(self):
        page = self._page("Title", " ".join(f"word{i}" for i in range(100)) + ".")
        script = build_narration_script(page, max_words=30)
        self.assertLessEqual(len(script.split()), 30)

    def test_title_only_when_no_intro(self):
        page = self._page("Just A Title", "")
        self.assertEqual(build_narration_script(page), "Just A Title.")

    def test_title_terminator_not_doubled(self):
        page = self._page("Already Ends!", "Intro here.")
        script = build_narration_script(page)
        self.assertTrue(script.startswith("Already Ends! Intro here."))
        self.assertNotIn("!.", script)
