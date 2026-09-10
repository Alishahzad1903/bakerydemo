from django.test import SimpleTestCase

from bakerydemo.videos.narration import build_narration_script, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_takes_up_to_first_terminator(self):
        self.assertEqual(
            first_sentence("One sentence. Two sentence. Three."), "One sentence."
        )

    def test_no_terminator_returns_whole_text(self):
        self.assertEqual(first_sentence("no punctuation here"), "no punctuation here")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")


class BuildNarrationScriptTests(SimpleTestCase):
    def test_title_plus_first_intro_sentence(self):
        script = build_narration_script(
            title="Tracking Wild Yeast",
            introduction="Yeasts are fascinating. They do many things.",
        )
        self.assertEqual(
            script, "Tracking Wild Yeast. Yeasts are fascinating."
        )

    def test_body_is_never_included(self):
        # Only title + first intro sentence — nothing else is narrated.
        script = build_narration_script(
            title="T", introduction="First sentence. Second sentence."
        )
        self.assertNotIn("Second", script)

    def test_word_cap_is_enforced(self):
        long_intro = " ".join(f"word{i}" for i in range(100)) + "."
        script = build_narration_script(
            title="Title", introduction=long_intro, max_words=30
        )
        self.assertLessEqual(len(script.split()), 30)

    def test_title_gets_terminal_punctuation(self):
        script = build_narration_script(title="A Title", introduction="Intro text.")
        self.assertTrue(script.startswith("A Title. "))

    def test_missing_intro_still_narrates_title(self):
        self.assertEqual(build_narration_script(title="Only Title", introduction=""), "Only Title.")
