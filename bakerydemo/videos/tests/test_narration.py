from django.test import SimpleTestCase, override_settings

from bakerydemo.videos.narration import build_script, cap_words, first_sentence


class FirstSentenceTests(SimpleTestCase):
    def test_extracts_first_sentence(self):
        text = (
            "Yeasts, with their single-celled growth habit, can be contrasted "
            "with molds, which grow hyphae. Fungal species that can take both "
            "forms are called dimorphic fungi."
        )
        self.assertEqual(
            first_sentence(text),
            "Yeasts, with their single-celled growth habit, can be contrasted "
            "with molds, which grow hyphae.",
        )

    def test_single_sentence_without_terminator(self):
        self.assertEqual(first_sentence("A short intro"), "A short intro")

    def test_collapses_whitespace(self):
        self.assertEqual(first_sentence("  a\n  b.  c"), "a b.")

    def test_empty(self):
        self.assertEqual(first_sentence(""), "")


class CapWordsTests(SimpleTestCase):
    def test_caps_to_limit(self):
        self.assertEqual(cap_words("one two three four", 2), "one two")

    def test_under_limit_unchanged(self):
        self.assertEqual(cap_words("one two", 5), "one two")


class BuildScriptTests(SimpleTestCase):
    def test_title_plus_first_sentence(self):
        script = build_script(
            "Tracking Wild Yeast",
            "Yeasts grow as single cells. They also do other things.",
        )
        self.assertEqual(script, "Tracking Wild Yeast. Yeasts grow as single cells.")

    def test_only_own_text_is_used_and_capped(self):
        # 30-word ceiling enforced.
        long_intro = " ".join(f"word{i}" for i in range(100)) + "."
        script = build_script("Title", long_intro, max_words=30)
        self.assertLessEqual(len(script.split()), 30)

    @override_settings(VIDEOGEN_MAX_SCRIPT_WORDS=5)
    def test_respects_settings_word_budget(self):
        script = build_script("A B C", "d e f g h i j.")
        self.assertEqual(len(script.split()), 5)

    def test_title_is_not_double_punctuated(self):
        script = build_script("Title?", "Intro sentence.")
        self.assertEqual(script, "Title. Intro sentence.")

    def test_raises_when_no_text(self):
        with self.assertRaises(ValueError):
            build_script("", "")
