from django.test import SimpleTestCase

from bakerydemo.videos.narration import MAX_WORDS, build_script, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_splits_on_first_period(self):
        self.assertEqual(
            first_sentence("One two three. Four five six."), "One two three."
        )

    def test_splits_on_question_and_exclamation(self):
        self.assertEqual(first_sentence("Really? Yes."), "Really?")
        self.assertEqual(first_sentence("Wow! Indeed."), "Wow!")

    def test_splits_on_newline(self):
        self.assertEqual(first_sentence("Line one\nLine two"), "Line one")

    def test_whole_text_when_no_boundary(self):
        self.assertEqual(first_sentence("no terminator here"), "no terminator here")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")


class BuildScriptTests(SimpleTestCase):
    def test_title_plus_first_sentence(self):
        script = build_script(
            "Tracking Wild Yeast",
            "Yeasts grow as single cells. They are fungi.",
        )
        self.assertEqual(
            script, "Tracking Wild Yeast. Yeasts grow as single cells."
        )

    def test_does_not_double_terminal_punctuation(self):
        script = build_script("Is it sourdough?", "Yes it is. And more.")
        self.assertEqual(script, "Is it sourdough? Yes it is.")

    def test_capped_at_max_words(self):
        long_intro = " ".join(f"w{i}" for i in range(100)) + "."
        script = build_script("Title", long_intro)
        self.assertLessEqual(len(script.split()), MAX_WORDS)

    def test_title_only_when_no_intro(self):
        self.assertEqual(build_script("Just a title", ""), "Just a title")

    def test_intro_only_when_no_title(self):
        self.assertEqual(build_script("", "Only intro. Second."), "Only intro.")

    def test_empty_inputs(self):
        self.assertEqual(build_script("", ""), "")
