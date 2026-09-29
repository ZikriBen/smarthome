import unittest

from app.classifier import (
    MediaType,
    classify_media,
)


class ClassifierTests(unittest.TestCase):
    def assert_episode(
        self,
        value: str,
        season: int | None,
        episode: int,
        *,
        episode_end: int | None = None,
        expected_type: MediaType = MediaType.TV,
    ) -> None:
        result = classify_media(
            filename=value,
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            expected_type,
        )

        self.assertIsNotNone(result.episode)
        assert result.episode is not None

        self.assertEqual(
            result.episode.season,
            season,
        )

        self.assertEqual(
            result.episode.episode_start,
            episode,
        )

        self.assertEqual(
            result.episode.episode_end,
            episode_end,
        )

    def test_standard_sxe(self):
        self.assert_episode(
            "Breaking.Bad.S03E07.1080p.mkv",
            3,
            7,
        )

    def test_short_sxe(self):
        self.assert_episode(
            "Show S1E2.mkv",
            1,
            2,
        )

    def test_x_format(self):
        self.assert_episode(
            "Show.Name.2x05.720p.mkv",
            2,
            5,
        )

    def test_english_verbose(self):
        self.assert_episode(
            "Show Season 2 Episode 5.mkv",
            2,
            5,
        )

    def test_english_ep(self):
        self.assert_episode(
            "Show Season 02 Ep 07.mkv",
            2,
            7,
        )

    def test_hebrew_verbose(self):
        self.assert_episode(
            "הסדרה עונה 2 פרק 5.mkv",
            2,
            5,
        )

    def test_hebrew_no_spaces(self):
        self.assert_episode(
            "הסדרה עונה2 פרק5.mkv",
            2,
            5,
        )

    def test_hebrew_reversed(self):
        self.assert_episode(
            "הסדרה פרק 5 עונה 2.mkv",
            2,
            5,
        )

    def test_hebrew_short(self):
        self.assert_episode(
            "הסדרה ע2 פ5.mkv",
            2,
            5,
        )

    def test_hebrew_short_quotes(self):
        self.assert_episode(
            "הסדרה ע'2 פ'5.mkv",
            2,
            5,
        )

    def test_multi_episode(self):
        self.assert_episode(
            "Show.S02E05E06.mkv",
            2,
            5,
            episode_end=6,
        )

    def test_multi_episode_dash(self):
        self.assert_episode(
            "Show.S02E05-06.mkv",
            2,
            5,
            episode_end=6,
        )

    def test_multi_episode_dash_e(self):
        self.assert_episode(
            "Show.S02E05-E06.mkv",
            2,
            5,
            episode_end=6,
        )

    def test_resolution_after_episode(self):
        self.assert_episode(
            "Show.S03E07.1080p.WEB-DL.mkv",
            3,
            7,
        )

    def test_filename_preferred_over_caption(self):
        result = classify_media(
            filename="Breaking.Bad.S04E08.mkv",
            caption="עונה 2 פרק 3",
        )

        self.assertEqual(
            result.media_type,
            MediaType.TV,
        )

        assert result.episode is not None

        self.assertEqual(
            result.episode.season,
            4,
        )

        self.assertEqual(
            result.episode.episode_start,
            8,
        )

        self.assertEqual(
            result.episode.source,
            "filename",
        )

    def test_episode_only_not_auto_classified(self):
        result = classify_media(
            filename="Episode 5.mkv",
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.UNKNOWN,
        )

        self.assertIsNotNone(result.episode)
        assert result.episode is not None

        self.assertEqual(
            result.episode.episode_start,
            5,
        )

        self.assertEqual(
            result.episode.confidence,
            0.60,
        )

    def test_hebrew_episode_only_not_auto_classified(self):
        result = classify_media(
            filename="פרק 12.mkv",
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.UNKNOWN,
        )

        assert result.episode is not None

        self.assertEqual(
            result.episode.episode_start,
            12,
        )

    def test_extract_english_show_title(self):
        result = classify_media(
            filename="Breaking.Bad.S03E07.1080p.WEB-DL.mkv",
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.TV,
        )

        self.assertEqual(
            result.title,
            "Breaking Bad",
        )

    def test_extract_hebrew_show_title(self):
        result = classify_media(
            filename="פאודה עונה 2 פרק 5 1080p.mkv",
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.TV,
        )

        self.assertEqual(
            result.title,
            "פאודה",
        )

    def test_extract_hebrew_title_with_translation_noise(self):
        result = classify_media(
            filename=(
                "קופה ראשית עונה 4 פרק 3 "
                "תרגום מובנה 720p.mkv"
            ),
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.TV,
        )

        self.assertEqual(
            result.title,
            "קופה ראשית",
        )

    def test_filename_title_preferred(self):
        result = classify_media(
            filename="The.Office.S02E05.1080p.WEBRip.mkv",
            caption="הפרק החדש של הסדרה",
        )

        self.assertEqual(
            result.title,
            "The Office",
        )

    def test_mentalist_filename_cleanup(self):
        result = classify_media(
            filename=(
                "720p WEB-DL השימיה "
                "המנטליסט ע3 פ21.mp4"
            ),
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.TV,
        )

        self.assertEqual(
            result.title,
            "המנטליסט",
        )

        assert result.episode is not None

        self.assertEqual(
            result.episode.season,
            3,
        )

        self.assertEqual(
            result.episode.episode_start,
            21,
        )

    def test_mentalist_caption_link_does_not_become_title(self):
        result = classify_media(
            filename=(
                "720p WEB-DL השימיה "
                "המנטליסט ע3 פ21.mp4"
            ),
            caption=(
                "[**המנטליסט**]"
                "(https://t.me/+YSofgZNVx71hNTY0**) "
                "- עונה 3 פרק 21\n"
                "**איכות:** 720p WEB-DL"
            ),
        )

        self.assertEqual(
            result.media_type,
            MediaType.TV,
        )

        self.assertEqual(
            result.title,
            "המנטליסט",
        )

        assert result.episode is not None

        self.assertEqual(
            result.episode.season,
            3,
        )

        self.assertEqual(
            result.episode.episode_start,
            21,
        )

    def test_movie_with_year_is_classified(self):
        result = classify_media(
            filename="Sunshine.2007.1080p.mkv",
            caption="Sunshine (2007)",
        )

        self.assertEqual(
            result.media_type,
            MediaType.MOVIE,
        )

        self.assertEqual(
            result.title,
            "Sunshine",
        )

        self.assertEqual(
            result.year,
            2007,
        )

    def test_movie_resolution_is_ignored(self):
        result = classify_media(
            filename="Movie.2026.720p.WEBRip.mkv",
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.MOVIE,
        )

        self.assertEqual(
            result.title,
            "Movie",
        )

        self.assertEqual(
            result.year,
            2026,
        )

    def test_movie_dragon_bruce_lee(self):
        result = classify_media(
            filename=(
                "לולו_סרטים_הדרקון:_סיפורו_של_ברוס_לי_"
                "1993_ת_מ_720P.mkv"
            ),
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.MOVIE,
        )

        self.assertEqual(
            result.title,
            "הדרקון: סיפורו של ברוס לי",
        )

        self.assertEqual(
            result.year,
            1993,
        )

    def test_movie_wedding_crashers(self):
        result = classify_media(
            filename=(
                "לולו_סרטים_לדפוק_חתונה_"
                "2005_ת.מ_720P.mkv"
            ),
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.MOVIE,
        )

        self.assertEqual(
            result.title,
            "לדפוק חתונה",
        )

        self.assertEqual(
            result.year,
            2005,
        )

    def test_movie_good_guys(self):
        result = classify_media(
            filename=(
                "לולו_סרטים_בחורים_טובים_"
                "2022_ישראלי_720P.mkv"
            ),
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.MOVIE,
        )

        self.assertEqual(
            result.title,
            "בחורים טובים",
        )

        self.assertEqual(
            result.year,
            2022,
        )

    def test_unknown_without_year_or_episode(self):
        result = classify_media(
            filename="random_video_file.mkv",
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.UNKNOWN,
        )

        self.assertIsNone(
            result.title,
        )

        self.assertIsNone(
            result.year,
        )


if __name__ == "__main__":
    unittest.main()
