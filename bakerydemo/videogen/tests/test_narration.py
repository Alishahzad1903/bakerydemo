from types import SimpleNamespace

from django.test import SimpleTestCase

from bakerydemo.videogen.narration import (
    MAX_WORDS,
    build_narration_script,
    first_sentence,
)


class NarrationTests(SimpleTestCase):
    def test_first_sentence_stops_at_first_terminator(self):
        text = "Wild yeast is everywhere. It floats on the breeze. And more."
        self.assertEqual(first_sentence(text), "Wild yeast is everywhere.")

    def test_first_sentence_handles_no_terminator(self):
        self.assertEqual(first_sentence("just some words"), "just some words")

    def test_first_sentence_empty(self):
        self.assertEqual(first_sentence("   "), "")

    def test_script_is_title_plus_first_intro_sentence(self):
        page = SimpleNamespace(
            title="Tracking Wild Yeast",
            introduction="Wild yeast is everywhere. Ignore this second sentence.",
            # body must never be read for narration:
            body="THIS BODY TEXT MUST NOT APPEAR",
        )
        script = build_narration_script(page)
        self.assertEqual(script, "Tracking Wild Yeast. Wild yeast is everywhere.")
        self.assertNotIn("BODY", script)

    def test_script_word_cap_enforced(self):
        page = SimpleNamespace(
            title="Title",
            introduction=" ".join(f"word{i}" for i in range(100)) + ".",
        )
        script = build_narration_script(page)
        self.assertLessEqual(len(script.split()), MAX_WORDS)

    def test_script_without_introduction_uses_title(self):
        page = SimpleNamespace(title="Only A Title", introduction="")
        self.assertEqual(build_narration_script(page), "Only A Title")

    def test_no_double_period_when_title_ends_with_terminator(self):
        page = SimpleNamespace(title="What is sourdough?", introduction="It is bread.")
        self.assertEqual(
            build_narration_script(page), "What is sourdough? It is bread."
        )
