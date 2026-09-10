from django.test import SimpleTestCase

from bakerydemo.videos.constants import MAX_NARRATION_WORDS
from bakerydemo.videos.narration import build_narration_script, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_extracts_first_sentence(self):
        self.assertEqual(
            first_sentence("Alpha beta. Gamma delta. Epsilon."),
            "Alpha beta.",
        )

    def test_collapses_whitespace_and_newlines(self):
        self.assertEqual(
            first_sentence("Alpha\n beta   gamma.\nMore text."),
            "Alpha beta gamma.",
        )

    def test_no_terminator_returns_whole_text(self):
        self.assertEqual(first_sentence("no terminator here"), "no terminator here")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")


class BuildNarrationTests(SimpleTestCase):
    def test_title_plus_first_intro_sentence(self):
        script = build_narration_script(
            title="Tracking Wild Yeast",
            introduction=(
                "Yeasts, with their single-celled growth habit, can be contrasted "
                "with molds, which grow hyphae. Fungal species that can take both "
                "forms are called dimorphic fungi."
            ),
        )
        self.assertEqual(
            script,
            "Tracking Wild Yeast. Yeasts, with their single-celled growth habit, "
            "can be contrasted with molds, which grow hyphae.",
        )

    def test_word_cap_enforced(self):
        script = build_narration_script(
            title="Word " * 20,
            introduction="Another " * 40 + ".",
        )
        self.assertLessEqual(len(script.split()), MAX_NARRATION_WORDS)

    def test_title_only_when_intro_empty(self):
        self.assertEqual(build_narration_script("My Title", ""), "My Title.")

    def test_title_keeps_existing_terminator(self):
        self.assertEqual(build_narration_script("Ready?", "Go now."), "Ready? Go now.")
