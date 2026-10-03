import unittest

from app.classifier import classify_media


class TestCorpusRules(unittest.TestCase):
    def test_reviewed_cases(self):
        # Input, type, title, season, first episode, last episode, year.
        cases = [
            ("480p זירה מדיה האמת ע1 פ2",
             "TV", "האמת", 1, 2, None, None),
            ("480p נריה סרטים בתולות ע3 פ1",
             "TV", "בתולות", 3, 1, None, None),
            ("480p WEB-DL 2023 זירה מדיה ההילולה",
             "MOVIE", "ההילולה", None, None, None, 2023),
            ("spongebob-squarepants.S7E3+4_480P",
             "TV", "spongebob-squarepants", 7, 3, 4, None),
            ("לולו_סרטים_קצה_חוט_אורון_ירדן_קץ_התמימות_2020_ישראלי_480p",
             "MOVIE", "קצה חוט אורון ירדן קץ התמימות",
             None, None, None, 2020),
            ("480p נריה סרטים קל ע1 פ1",
             "TV", "קל", 1, 1, None, None),
            ("480p נריה סרטים גאון מרושע פ1",
             "TV", "גאון מרושע", 1, 1, None, None),
            ("לולו_סרטים_אברהם_פריד_הופעה_במנצ'סטר_2015_480P",
             "MOVIE", "אברהם פריד הופעה במנצ'סטר",
             None, None, None, 2015),
            ("לולו_סרטים_טקס_הדלקת_המשואות_2023_480p",
             "MOVIE", "טקס הדלקת המשואות", None, None, None, 2023),
            ("V_Wars.S1E1_480P",
             "TV", "V Wars", 1, 1, None, None),
            ("480p בוג'ק הורסמן ע1 פ1 נריה סרטים",
             "TV", "בוג'ק הורסמן", 1, 1, None, None),
            ("480p BluRay ז.מ כנופיית ברמינגהם ע4 פ1.mkv",
             "TV", "כנופיית ברמינגהם", 4, 1, None, None),
            ("Total Dhamaal (2019) HDRip 480p קבוצת סרטים",
             "MOVIE", "Total Dhamaal", None, None, None, 2019),
            ("The Replacements S01E02.mkv",
             "TV", "The Replacements", 1, 2, None, None),
            ("Show S03E07-E08.mkv",
             "TV", "Show", 3, 7, 8, None),
            ("Show Episode 7.mkv",
             "TV", "Show", 1, 7, None, None),
            ("סדרה פרק 7.mkv",
             "TV", "סדרה", 1, 7, None, None),
        ]
        for text, *expected in cases:
            with self.subTest(input=text):
                result = classify_media(filename=text, caption=None)
                episode = result.episode
                actual = [
                    result.media_type.value,
                    result.title,
                    episode.season if episode else None,
                    episode.episode_start if episode else None,
                    episode.episode_end if episode else None,
                    result.year,
                ]
                self.assertEqual(actual, expected)

    def test_parts_do_not_imply_a_season(self):
        for text in (
            "480p נריה סרטים איה בני חלק 1",
            "480p היסטוריית גיבורי העל ח1",
        ):
            with self.subTest(input=text):
                result = classify_media(filename=text, caption=None)
                self.assertEqual(result.media_type.value, "UNKNOWN")
                self.assertIsNone(result.episode)

    def test_assumed_season_and_confidence(self):
        result = classify_media(filename="Show Ep 7.mkv", caption=None)
        self.assertEqual(result.episode.season, 1)
        self.assertEqual(result.episode.confidence, 0.85)
        self.assertTrue(
            result.episode.pattern.endswith("_assumed_season_1")
        )

        result = classify_media(
            filename="Show Ep 7.mkv",
            caption=None,
            auto_tv_threshold=0.9,
        )
        self.assertEqual(result.media_type.value, "UNKNOWN")

    def test_explicit_season_wins(self):
        result = classify_media(
            filename="Show Ep 7.mkv",
            caption="Show S03E07",
        )
        self.assertEqual(result.episode.season, 3)
        self.assertNotIn("assumed", result.episode.pattern)


if __name__ == "__main__":
    unittest.main()
