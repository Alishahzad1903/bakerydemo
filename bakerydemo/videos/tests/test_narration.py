from django.test import SimpleTestCase

from bakerydemo.videos.narration import (
    MAX_WORDS,
    build_narration_script,
    first_sentence,
)


class FirstSentenceTests(SimpleTestCase):
    def test_takes_only_the_first_sentence(self):
        text = "This is one. This is two. This is three."
        self.assertEqual(first_sentence(text), "This is one.")

    def test_collapses_whitespace_and_newlines(self):
        self.assertEqual(first_sentence("Hello\n  world.  Next."), "Hello world.")

    def test_handles_no_terminal_punctuation(self):
        self.assertEqual(first_sentence("A single clause"), "A single clause")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")


class BuildNarrationScriptTests(SimpleTestCase):
    def test_combines_title_and_first_sentence(self):
        script = build_narration_script(
            title="Tracking Wild Yeast",
            introduction="Yeasts are single-celled fungi. They are everywhere.",
        )
        self.assertEqual(script, "Tracking Wild Yeast. Yeasts are single-celled fungi.")

    def test_never_exceeds_word_cap(self):
        intro = " ".join(f"word{i}" for i in range(100)) + "."
        script = build_narration_script(
            title="Some Long Title Here", introduction=intro
        )
        self.assertLessEqual(len(script.split()), MAX_WORDS)

    def test_title_only_when_no_introduction(self):
        script = build_narration_script(title="Just A Title", introduction="")
        self.assertEqual(script, "Just A Title.")

    def test_does_not_double_terminal_punctuation(self):
        script = build_narration_script(
            title="Ends already!", introduction="Body here."
        )
        self.assertEqual(script, "Ends already! Body here.")
