import unittest

from app.classifier import (
    MediaType,
    classify_media,
)


class ClassifierMetadataTests(
    unittest.TestCase
):
    def test_dotted_metadata_before_hebrew_title(
        self,
    ):
        result = classify_media(
            filename=(
                "480p BluRay ז.מ "
                "כנופיית ברמינגהם "
                "ע4 פ1.mkv"
            ),
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.TV,
        )

        self.assertEqual(
            result.title,
            "כנופיית ברמינגהם",
        )

        self.assertIsNotNone(
            result.episode
        )

        assert result.episode is not None

        self.assertEqual(
            result.episode.season,
            4,
        )

        self.assertEqual(
            result.episode.episode_start,
            1,
        )


    def test_generic_dotted_metadata_prefix(
        self,
    ):
        result = classify_media(
            filename=(
                "720p א.ב "
                "הסדרה שלי "
                "ע2 פ3.mkv"
            ),
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.TV,
        )

        self.assertEqual(
            result.title,
            "הסדרה שלי",
        )


    def test_single_letter_title_prefix_is_preserved(
        self,
    ):
        result = classify_media(
            filename=(
                "V הסדרה "
                "ע2 פ3.mkv"
            ),
            caption=None,
        )

        self.assertEqual(
            result.media_type,
            MediaType.TV,
        )

        self.assertEqual(
            result.title,
            "V הסדרה",
        )


    def test_existing_mentalist_case_still_works(
        self,
    ):
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


if __name__ == "__main__":
    unittest.main()
