import unittest

from app.classifier import (
    ClassificationResult,
    EpisodeMatch,
    MediaType,
)
from app.media_paths import (
    build_media_path,
    build_tv_path,
)


class MediaPathTests(unittest.TestCase):
    def test_standard_tv_path(self):
        classification = ClassificationResult(
            media_type=MediaType.TV,
            title="Breaking Bad",
            episode=EpisodeMatch(
                season=3,
                episode_start=7,
                episode_end=None,
                confidence=1.0,
                pattern="sxe",
                matched_text="S03E07",
                source="filename",
            ),
        )

        path = build_tv_path(
            media_root="/media",
            classification=classification,
            original_filename="Breaking.Bad.S03E07.1080p.mkv",
        )

        self.assertEqual(
            str(path),
            (
                "/media/tv/Breaking Bad/"
                "Season 03/"
                "Breaking Bad - S03E07.mkv"
            ),
        )

    def test_hebrew_tv_path(self):
        classification = ClassificationResult(
            media_type=MediaType.TV,
            title="פאודה",
            episode=EpisodeMatch(
                season=2,
                episode_start=5,
                episode_end=None,
                confidence=1.0,
                pattern="hebrew_verbose",
                matched_text="עונה 2 פרק 5",
                source="filename",
            ),
        )

        path = build_tv_path(
            media_root="/media",
            classification=classification,
            original_filename="פאודה עונה 2 פרק 5.mkv",
        )

        self.assertEqual(
            str(path),
            (
                "/media/tv/פאודה/"
                "Season 02/"
                "פאודה - S02E05.mkv"
            ),
        )

    def test_multi_episode_path(self):
        classification = ClassificationResult(
            media_type=MediaType.TV,
            title="Show",
            episode=EpisodeMatch(
                season=2,
                episode_start=5,
                episode_end=6,
                confidence=1.0,
                pattern="sxe_multi",
                matched_text="S02E05E06",
                source="filename",
            ),
        )

        path = build_tv_path(
            media_root="/media",
            classification=classification,
            original_filename="Show.S02E05E06.mp4",
        )

        self.assertEqual(
            str(path),
            (
                "/media/tv/Show/"
                "Season 02/"
                "Show - S02E05-E06.mp4"
            ),
        )

    def test_path_component_sanitizing(self):
        classification = ClassificationResult(
            media_type=MediaType.TV,
            title="Show / Name",
            episode=EpisodeMatch(
                season=1,
                episode_start=1,
                episode_end=None,
                confidence=1.0,
                pattern="sxe",
                matched_text="S01E01",
                source="filename",
            ),
        )

        path = build_tv_path(
            media_root="/media",
            classification=classification,
            original_filename="whatever.mkv",
        )

        self.assertEqual(
            str(path),
            (
                "/media/tv/Show _ Name/"
                "Season 01/"
                "Show _ Name - S01E01.mkv"
            ),
        )

    def test_build_media_path_dispatch(self):
        classification = ClassificationResult(
            media_type=MediaType.TV,
            title="The Office",
            episode=EpisodeMatch(
                season=2,
                episode_start=5,
                episode_end=None,
                confidence=1.0,
                pattern="sxe",
                matched_text="S02E05",
                source="filename",
            ),
        )

        path = build_media_path(
            media_root="/media",
            classification=classification,
            original_filename="The.Office.S02E05.mkv",
        )

        self.assertEqual(
            str(path),
            (
                "/media/tv/The Office/"
                "Season 02/"
                "The Office - S02E05.mkv"
            ),
        )

    def test_unknown_media_rejected(self):
        classification = ClassificationResult(
            media_type=MediaType.UNKNOWN,
            title=None,
            episode=None,
        )

        with self.assertRaises(ValueError):
            build_media_path(
                media_root="/media",
                classification=classification,
                original_filename="Movie.mkv",
            )

    def test_movie_path(self):
        classification = ClassificationResult(
            media_type=MediaType.MOVIE,
            title="לדפוק חתונה",
            year=2005,
        )

        path = build_media_path(
            media_root="/media",
            classification=classification,
            original_filename="whatever.mkv",
        )

        self.assertEqual(
            str(path),
            (
                "/media/movies/"
                "לדפוק חתונה (2005)/"
                "לדפוק חתונה (2005).mkv"
            ),
        )

if __name__ == "__main__":
    unittest.main()
