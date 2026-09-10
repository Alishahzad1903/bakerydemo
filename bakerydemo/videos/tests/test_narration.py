from django.test import SimpleTestCase

from bakerydemo.videos.narration import build_narration_script, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_takes_first_sentence(self):
        text = "Yeasts are fascinating. They are single-celled. And ancient."
        self.assertEqual(first_sentence(text), "Yeasts are fascinating.")

    def test_handles_question_and_exclamation(self):
        self.assertEqual(first_sentence("Really? Yes."), "Really?")
        self.assertEqual(first_sentence("Wow! Amazing."), "Wow!")

    def test_blank(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence("   "), "")

    def test_single_sentence_without_terminator(self):
        self.assertEqual(first_sentence("no period here"), "no period here")


class BuildNarrationScriptTests(SimpleTestCase):
    def test_combines_title_and_first_sentence(self):
        script = build_narration_script(
            "Tracking Wild Yeast",
            "Yeasts, with their single-celled growth habit, can be contradictory. More text.",
        )
        self.assertTrue(script.startswith("Tracking Wild Yeast."))
        self.assertIn("single-celled growth habit", script)
        # Only the first sentence of the intro is used.
        self.assertNotIn("More text", script)

    def test_never_exceeds_the_word_ceiling(self):
        intro = " ".join(f"word{i}" for i in range(100)) + "."
        script = build_narration_script("A Very Long Article Title Here", intro)
        self.assertLessEqual(len(script.split()), 30)

    def test_custom_max_words(self):
        script = build_narration_script(
            "Title", "one two three four five six.", max_words=4
        )
        self.assertEqual(len(script.split()), 4)

    def test_title_only_when_intro_blank(self):
        self.assertEqual(build_narration_script("Just A Title", ""), "Just A Title.")

    def test_title_terminator_not_duplicated(self):
        self.assertTrue(build_narration_script("Ends already!", "Body.").startswith("Ends already!"))
        self.assertNotIn("already!.", build_narration_script("Ends already!", "Body."))
