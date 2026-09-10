from django.test import SimpleTestCase

from bakerydemo.videos.narration import build_narration_script, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_extracts_first_sentence_keeping_terminator(self):
        self.assertEqual(
            first_sentence("Wild yeast is everywhere. It floats on the breeze."),
            "Wild yeast is everywhere.",
        )

    def test_handles_question_and_exclamation(self):
        self.assertEqual(first_sentence("Ready to bake? Let's go!"), "Ready to bake?")

    def test_blank_input(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")

    def test_single_sentence_without_terminator(self):
        self.assertEqual(first_sentence("Just some text"), "Just some text")


class BuildNarrationScriptTests(SimpleTestCase):
    def test_combines_title_and_first_intro_sentence(self):
        script = build_narration_script(
            "Tracking Wild Yeast",
            "Wild yeast is everywhere. The rest of the introduction is ignored.",
        )
        self.assertEqual(
            script, "Tracking Wild Yeast. Wild yeast is everywhere."
        )

    def test_only_first_sentence_of_introduction_is_used(self):
        script = build_narration_script(
            "Title",
            "One. Two. Three.",
        )
        self.assertNotIn("Two", script)
        self.assertNotIn("Three", script)

    def test_word_ceiling_is_enforced(self):
        long_intro = "word " * 100
        script = build_narration_script("A Title", long_intro, max_words=30)
        self.assertLessEqual(len(script.split()), 30)

    def test_title_only_when_no_introduction(self):
        self.assertEqual(build_narration_script("Just A Title", ""), "Just A Title.")

    def test_title_terminator_not_duplicated(self):
        self.assertEqual(
            build_narration_script("Question?", "Answer here."),
            "Question? Answer here.",
        )
