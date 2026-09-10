from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from bakerydemo.videogen.narration import build_narration_script, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_returns_first_sentence(self):
        text = "Yeasts grow as single cells. Molds grow hyphae. A third one."
        self.assertEqual(first_sentence(text), "Yeasts grow as single cells.")

    def test_collapses_whitespace(self):
        self.assertEqual(first_sentence("  Hello   world.\nNext. "), "Hello world.")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")


class BuildNarrationScriptTests(SimpleTestCase):
    def _page(self, title="", introduction=""):
        return SimpleNamespace(title=title, introduction=introduction)

    def test_title_plus_first_intro_sentence_only(self):
        page = self._page(
            title="Tracking Wild Yeast",
            introduction=(
                "Yeasts, with their single-celled growth habit, can be contrasted "
                "with molds, which grow hyphae. This second sentence is ignored."
            ),
        )
        script = build_narration_script(page)
        self.assertEqual(
            script,
            "Tracking Wild Yeast. Yeasts, with their single-celled growth habit, "
            "can be contrasted with molds, which grow hyphae.",
        )
        # The body / later sentences are never included.
        self.assertNotIn("second sentence", script)

    def test_title_only_when_no_intro(self):
        self.assertEqual(build_narration_script(self._page(title="Just A Title")), "Just A Title.")

    def test_does_not_double_terminal_punctuation(self):
        page = self._page(title="Ready?", introduction="Go now.")
        self.assertEqual(build_narration_script(page), "Ready? Go now.")

    @override_settings(VIDEOGEN_MAX_SCRIPT_WORDS=5)
    def test_truncates_to_max_words(self):
        page = self._page(
            title="One Two Three",
            introduction="Four five six seven eight nine.",
        )
        script = build_narration_script(page)
        self.assertLessEqual(len(script.split()), 5)
        self.assertTrue(script.endswith((".", "!", "?")))

    def test_default_ceiling_is_thirty_words(self):
        page = self._page(
            title="Title",
            introduction=" ".join(f"word{i}" for i in range(100)) + ".",
        )
        self.assertLessEqual(len(build_narration_script(page).split()), 30)
