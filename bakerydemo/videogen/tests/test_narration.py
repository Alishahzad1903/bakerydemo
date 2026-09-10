from django.test import SimpleTestCase

from bakerydemo.videogen.narration import (
    MAX_SCRIPT_WORDS,
    build_narration_script,
    first_sentence,
)


class FirstSentenceTests(SimpleTestCase):
    def test_stops_at_first_period(self):
        self.assertEqual(
            first_sentence("One thing. Two thing. Three."),
            "One thing.",
        )

    def test_handles_question_and_exclamation(self):
        self.assertEqual(first_sentence("Really? Yes."), "Really?")
        self.assertEqual(first_sentence("Wow! Great."), "Wow!")

    def test_no_terminator_returns_whole_text(self):
        self.assertEqual(first_sentence("no full stop here"), "no full stop here")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")

    def test_does_not_split_decimal_without_space(self):
        # A period not followed by whitespace is not a sentence boundary.
        self.assertEqual(
            first_sentence("Version 3.12 shipped. Later."), "Version 3.12 shipped."
        )


class BuildNarrationScriptTests(SimpleTestCase):
    def test_combines_title_and_first_intro_sentence(self):
        script = build_narration_script(
            "Tracking Wild Yeast",
            "Yeasts can be contrasted with molds. They grow hyphae.",
        )
        self.assertEqual(
            script,
            "Tracking Wild Yeast. Yeasts can be contrasted with molds.",
        )

    def test_excludes_body_and_later_sentences(self):
        script = build_narration_script(
            "Title",
            "First sentence. Second sentence should not appear.",
        )
        self.assertNotIn("Second sentence", script)

    def test_caps_at_max_words(self):
        intro = " ".join(f"word{i}" for i in range(100)) + "."
        script = build_narration_script("A Long Title Here", intro)
        self.assertLessEqual(len(script.split()), MAX_SCRIPT_WORDS)

    def test_uses_only_provided_words(self):
        # Every word in the script must come from the title or the intro's first
        # sentence — nothing invented.
        title = "Sourdough Basics"
        intro = "Wild yeast lives everywhere. More text."
        script = build_narration_script(title, intro)
        allowed = set(title.split()) | set("Wild yeast lives everywhere.".split())
        for word in script.replace(".", " ").split():
            self.assertIn(word, {w.replace(".", "") for w in allowed} | allowed)

    def test_title_only_when_no_intro(self):
        self.assertEqual(build_narration_script("Just A Title", ""), "Just A Title")

    def test_title_already_punctuated(self):
        script = build_narration_script("Is it ready?", "Yes it is. No.")
        self.assertEqual(script, "Is it ready? Yes it is.")
