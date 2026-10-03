import unittest

from app.classifier import MediaType, classify_media


class TestClassifierRound3(unittest.TestCase):
    """
    Agreed policy: downloadable content is assumed to be a movie unless
    it carries explicit season/episode evidence. These are explicit
    expectations against the corpus, not generated from predictions.
    """

    def test_yearless_documentary_titles_become_movies(self):
        cases = [
            ("לולו-סרטים-מוות_בלי_תשובה_480p", "מוות בלי תשובה"),
            (
                "לולו_סרטים_ישראל_על_הירח_המשדר_המלא_480p",
                "ישראל על הירח המשדר המלא",
            ),
            (
                "לולו_סרטים_חיסול_ממוקד_באיראן_אלון_בן_דוד_480p",
                "חיסול ממוקד באיראן אלון בן דוד",
            ),
            ("לולו_סרטים_סאבוטז'_480p", "סאבוטז'"),
        ]
        for text, title in cases:
            with self.subTest(text=text):
                result = classify_media(filename=text, caption=None)
                self.assertEqual(result.media_type, MediaType.MOVIE)
                self.assertEqual(result.title, title)
                self.assertIsNone(result.year)
                self.assertEqual(result.movie.confidence, 0.90)

    def test_explicit_movie_word_still_wins_regardless_of_year(self):
        result = classify_media(
            filename="ענבל אור הסרט דב סרטים 480p",
            caption=None,
        )
        self.assertEqual(result.media_type, MediaType.MOVIE)
        self.assertEqual(result.title, "ענבל אור הסרט דב סרטים")

    def test_multipart_markers_still_excluded_from_movie_fallback(self):
        # Agreed policy: "חלק 1"/"ח1"/"CD2" do not imply TV, but tying
        # multiple parts into one movie record is unsolved deferred work.
        # These must stay UNKNOWN, not silently become a standalone movie.
        for text in (
            "480p נריה סרטים איה בני חלק 1",
            "480p היסטוריית גיבורי העל ח1",
        ):
            with self.subTest(text=text):
                result = classify_media(filename=text, caption=None)
                self.assertEqual(result.media_type, MediaType.UNKNOWN)

    def test_letter_suffixed_parts_become_separate_movies(self):
        # Unlike digit-based multipart markers, a letter suffix (א/ב) here
        # is part of the title itself (e.g. "Part A"/"Part B" of a named
        # documentary), not a season/episode marker, so each becomes its
        # own movie. This is current behavior, not verified ground truth.
        result_a = classify_media(
            filename="לולו_סרטים_הלילה_הארוך_ביותר_חלק_א_480p",
            caption=None,
        )
        result_b = classify_media(
            filename="לולו_סרטים_הלילה_הארוך_ביותר_חלק_ב_480p",
            caption=None,
        )
        self.assertEqual(result_a.media_type, MediaType.MOVIE)
        self.assertEqual(result_b.media_type, MediaType.MOVIE)
        self.assertEqual(result_a.title, "הלילה הארוך ביותר חלק א")
        self.assertEqual(result_b.title, "הלילה הארוך ביותר חלק ב")

    def test_known_risk_named_episodes_without_numeric_evidence(self):
        # KNOWN RISK, not verified ground truth: these read like named TV
        # episodes of a recurring show ("Space Rangers", a dated recurring
        # segment), but carry no numeric season/episode marker for the
        # classifier to catch, so the movie-by-default policy currently
        # misclassifies them as standalone movies. Flagged here so future
        # changes (e.g. TMDb lookups, named-episode heuristics) have an
        # explicit before/after to compare against, instead of silently
        # drifting. Do not "fix" this test by weakening the assertions
        # without addressing the underlying named-episode gap.
        for text in (
            "סיירי החלל היום של נץ 480p",
            "480p זירה מדיה מהצד השני 27.10.22",
        ):
            with self.subTest(text=text):
                result = classify_media(filename=text, caption=None)
                self.assertEqual(result.media_type, MediaType.MOVIE)

    def test_garbage_and_too_short_titles_stay_unknown(self):
        for text in ("+YSofgZNVx71hNTY0.mkv", "a.mkv"):
            with self.subTest(text=text):
                result = classify_media(filename=text, caption=None)
                self.assertEqual(result.media_type, MediaType.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
