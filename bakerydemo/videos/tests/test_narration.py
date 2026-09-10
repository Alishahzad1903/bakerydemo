from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from bakerydemo.videos.narration import build_narration_script, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_takes_first_sentence_only(self):
        text = "Yeasts can be contrasted with molds. They grow differently. And more."
        self.assertEqual(
            first_sentence(text), "Yeasts can be contrasted with molds."
        )

    def test_normalises_whitespace(self):
        self.assertEqual(first_sentence("  Hello   world.\nNext."), "Hello world.")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence(None), "")


class BuildNarrationTests(SimpleTestCase):
    def _page(self, title, introduction):
        return SimpleNamespace(title=title, introduction=introduction)

    def test_title_plus_first_intro_sentence(self):
        page = self._page(
            "Tracking Wild Yeast",
            "Yeasts can be contrasted with molds. A second sentence is ignored.",
        )
        self.assertEqual(
            build_narration_script(page),
            "Tracking Wild Yeast. Yeasts can be contrasted with molds.",
        )

    def test_body_is_never_included(self):
        # Even if a page exposes a body attribute, it must not appear.
        page = SimpleNamespace(
            title="Title",
            introduction="Intro sentence here.",
            body="BODYTEXT should never be narrated",
        )
        script = build_narration_script(page)
        self.assertNotIn("BODYTEXT", script)
        self.assertEqual(script, "Title. Intro sentence here.")

    @override_settings(VIDEOGEN_NARRATION_MAX_WORDS=5)
    def test_word_ceiling_is_enforced(self):
        page = self._page("One Two", "Three four five six seven eight nine.")
        script = build_narration_script(page)
        self.assertEqual(len(script.split()), 5)

    def test_title_only_when_no_intro(self):
        page = self._page("Just A Title", "")
        self.assertEqual(build_narration_script(page), "Just A Title.")

    def test_keeps_existing_title_punctuation(self):
        page = self._page("Is this bread?", "Yes it is. Indeed.")
        self.assertEqual(build_narration_script(page), "Is this bread? Yes it is.")
