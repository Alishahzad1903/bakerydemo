from django.test import SimpleTestCase

from bakerydemo.video.narration import (
    MAX_WORDS,
    build_narration_script,
    first_sentence,
)


class NarrationTests(SimpleTestCase):
    def test_joins_title_and_first_intro_sentence(self):
        script = build_narration_script(
            title="Tracking Wild Yeast",
            introduction="Wild yeast is everywhere. A second sentence here.",
        )
        self.assertEqual(script, "Tracking Wild Yeast. Wild yeast is everywhere")

    def test_body_is_never_used(self):
        # The builder only receives title and introduction; there is no channel
        # through which the body could reach the narration.
        script = build_narration_script(
            title="Title", introduction="Intro sentence one. Intro sentence two."
        )
        self.assertNotIn("two", script)

    def test_caps_at_max_words(self):
        long_intro = " ".join(f"word{i}" for i in range(50)) + "."
        script = build_narration_script(title="A Title", introduction=long_intro)
        self.assertLessEqual(len(script.split()), MAX_WORDS)

    def test_first_sentence_without_terminator(self):
        self.assertEqual(first_sentence("no terminator here"), "no terminator here")

    def test_first_sentence_collapses_whitespace(self):
        self.assertEqual(first_sentence("  lots   of\n\nspace. x"), "lots of space")

    def test_title_only_when_introduction_empty(self):
        self.assertEqual(build_narration_script("Just a Title", ""), "Just a Title")
