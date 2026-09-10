from django.test import TestCase

from bakerydemo.videos.narration import (
    build_narration_script,
    first_sentence,
)


class _FakePage:
    def __init__(self, title="", introduction=""):
        self.title = title
        self.introduction = introduction
        self.body = []


class FirstSentenceTests(TestCase):
    def test_takes_only_the_first_sentence(self):
        text = "One two three. Four five six. Seven."
        self.assertEqual(first_sentence(text), "One two three.")

    def test_collapses_whitespace(self):
        self.assertEqual(first_sentence("  hello\n  world.  rest"), "hello world.")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")


class BuildNarrationScriptTests(TestCase):
    def test_title_plus_first_sentence_of_intro(self):
        page = _FakePage(
            title="Tracking Wild Yeast",
            introduction=(
                "Yeasts, with their single-celled growth habit, can be "
                "contrasted with molds, which grow hyphae. A second sentence "
                "that must not appear."
            ),
        )
        script = build_narration_script(page, max_words=30)
        self.assertTrue(script.startswith("Tracking Wild Yeast."))
        self.assertIn("grow hyphae", script)
        self.assertNotIn("second sentence", script.lower())

    def test_respects_word_cap(self):
        page = _FakePage(
            title="Title Here",
            introduction=" ".join(f"word{i}" for i in range(100)) + ".",
        )
        script = build_narration_script(page, max_words=10)
        self.assertLessEqual(len(script.split()), 10)
        self.assertTrue(script.endswith("."))

    def test_ends_with_terminal_punctuation(self):
        page = _FakePage(title="No Punctuation Title", introduction="")
        script = build_narration_script(page, max_words=30)
        self.assertTrue(script.endswith("."))

    def test_raises_without_any_text(self):
        with self.assertRaises(ValueError):
            build_narration_script(_FakePage(), max_words=30)
