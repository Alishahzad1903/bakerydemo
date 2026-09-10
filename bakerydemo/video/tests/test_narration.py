from django.test import SimpleTestCase

from bakerydemo.video.narration import MAX_WORDS, build_script


class BuildScriptTests(SimpleTestCase):
    def test_title_and_first_sentence_only(self):
        script = build_script(
            title="Tracking Wild Yeast",
            introduction="Yeasts are fascinating. They are everywhere. The end.",
        )
        self.assertEqual(script, "Tracking Wild Yeast. Yeasts are fascinating.")

    def test_only_first_sentence_of_introduction_is_used(self):
        script = build_script(
            title="A",
            introduction="First sentence here. Second should be dropped.",
        )
        self.assertNotIn("Second", script)
        self.assertIn("First sentence here.", script)

    def test_empty_introduction_uses_title_only(self):
        self.assertEqual(build_script(title="Just A Title", introduction=""), "Just A Title.")

    def test_title_existing_punctuation_preserved(self):
        self.assertEqual(
            build_script(title="Is it bread?", introduction="Yes it is."),
            "Is it bread? Yes it is.",
        )

    def test_html_is_stripped(self):
        script = build_script(
            title="<b>Bold</b> Title",
            introduction="<p>Intro <em>text</em> here.</p> More.",
        )
        self.assertEqual(script, "Bold Title. Intro text here.")

    def test_word_count_is_capped(self):
        intro = "word " * 100  # 100 words, one run-on "sentence"
        script = build_script(title="Title", introduction=intro)
        self.assertLessEqual(len(script.split()), MAX_WORDS)

    def test_realistic_article_is_short(self):
        script = build_script(
            title="Tracking Wild Yeast",
            introduction=(
                "Yeasts, with their single-celled growth habit, can be contrasted "
                "with molds, which grow hyphae. Fungal species that can take both "
                "forms are called dimorphic fungi."
            ),
        )
        self.assertTrue(script.startswith("Tracking Wild Yeast."))
        self.assertLessEqual(len(script.split()), MAX_WORDS)
        self.assertNotIn("dimorphic", script)
