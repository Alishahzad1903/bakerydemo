from django.test import SimpleTestCase

from bakerydemo.videos.narration import MAX_NARRATION_WORDS, build_narration_script


class NarrationTests(SimpleTestCase):
    def test_title_plus_first_sentence(self):
        script = build_narration_script(
            title="Tracking Wild Yeast",
            introduction=(
                "Yeasts, with their single-celled growth habit, can be "
                "contrasted with molds, which grow hyphae. A second sentence "
                "that must not appear."
            ),
        )
        self.assertEqual(
            script,
            "Tracking Wild Yeast. Yeasts, with their single-celled growth "
            "habit, can be contrasted with molds, which grow hyphae.",
        )
        self.assertNotIn("second sentence", script.lower())

    def test_word_cap_enforced(self):
        intro = " ".join(f"word{i}" for i in range(100)) + "."
        script = build_narration_script(title="Big Title Here", introduction=intro)
        self.assertLessEqual(len(script.split()), MAX_NARRATION_WORDS)
        # The title is always preserved.
        self.assertTrue(script.startswith("Big Title Here."))

    def test_empty_introduction_uses_title_only(self):
        script = build_narration_script(title="Just A Title", introduction="")
        self.assertEqual(script, "Just A Title.")

    def test_whitespace_is_collapsed(self):
        script = build_narration_script(
            title="  Spaced   Title  ",
            introduction="First\n\n  sentence here.  Second one.",
        )
        self.assertEqual(script, "Spaced Title. First sentence here.")
