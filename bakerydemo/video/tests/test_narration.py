from django.test import SimpleTestCase

from bakerydemo.video.narration import (
    MAX_SCRIPT_WORDS,
    build_script,
    first_sentence,
)


class NarrationTests(SimpleTestCase):
    def test_first_sentence_splits_on_boundary(self):
        text = "First one. Second one. Third one."
        self.assertEqual(first_sentence(text), "First one.")

    def test_first_sentence_handles_no_terminator(self):
        self.assertEqual(first_sentence("no full stop here"), "no full stop here")

    def test_first_sentence_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")

    def test_build_script_title_and_first_sentence(self):
        script = build_script(
            "Tracking Wild Yeast",
            "Yeasts grow as single cells. They are fungi. More text here.",
        )
        self.assertEqual(script, "Tracking Wild Yeast. Yeasts grow as single cells.")

    def test_build_script_uses_only_first_sentence_not_body(self):
        script = build_script(
            "Title", "Sentence one is here. Sentence two must be excluded."
        )
        self.assertIn("Sentence one is here.", script)
        self.assertNotIn("Sentence two", script)

    def test_build_script_title_only_when_no_intro(self):
        self.assertEqual(build_script("Just A Title", ""), "Just A Title.")

    def test_build_script_respects_word_cap(self):
        long_intro = " ".join(f"word{i}" for i in range(100)) + "."
        script = build_script("A B C", long_intro)
        self.assertLessEqual(len(script.split()), MAX_SCRIPT_WORDS)

    def test_build_script_does_not_double_punctuate(self):
        self.assertTrue(build_script("Title?", "Intro.").startswith("Title?"))
