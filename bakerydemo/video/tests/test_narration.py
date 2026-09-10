from django.test import SimpleTestCase

from bakerydemo.video.narration import (
    MAX_WORDS,
    build_narration_script,
    first_sentence,
)


class FirstSentenceTests(SimpleTestCase):
    def test_splits_on_first_terminator(self):
        self.assertEqual(
            first_sentence("One sentence here. And a second one."),
            "One sentence here",
        )

    def test_collapses_whitespace(self):
        self.assertEqual(first_sentence("  Spread   out   words.  "), "Spread out words")

    def test_handles_no_terminator(self):
        self.assertEqual(first_sentence("No terminator at all"), "No terminator at all")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")


class BuildNarrationScriptTests(SimpleTestCase):
    def test_title_and_first_sentence_only(self):
        script = build_narration_script(
            title="Tracking Wild Yeast",
            introduction=(
                "Yeasts, with their single-celled growth habit, can be contrasted "
                "with molds, which grow hyphae. Fungal species that can take both "
                "forms are called dimorphic."
            ),
        )
        self.assertEqual(
            script,
            "Tracking Wild Yeast. Yeasts, with their single-celled growth habit, "
            "can be contrasted with molds, which grow hyphae.",
        )
        # The body's second sentence must never leak into the narration.
        self.assertNotIn("dimorphic", script)

    def test_never_exceeds_word_cap(self):
        long_intro = " ".join(f"word{i}" for i in range(100)) + "."
        script = build_narration_script(title="A Very Long Title", introduction=long_intro)
        self.assertLessEqual(len(script.split()), MAX_WORDS)

    def test_title_only_when_no_introduction(self):
        self.assertEqual(
            build_narration_script(title="Just a Title", introduction=""),
            "Just a Title.",
        )
