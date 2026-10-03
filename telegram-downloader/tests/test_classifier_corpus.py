import json
import unittest
from collections import Counter
from pathlib import Path

from app.classifier import MediaType, classify_media


CORPUS_PATH = (
    Path(__file__).parent / "fixtures" / "corpus-480p.jsonl"
)

# Explicit expectations, never generated from classifier predictions.
# Add reviewed cases here as we investigate recurring issues.
CASES = [
    {
        "input": "לולו_סרטים_האח_הגדול_ע15פ26_הזדמנות_שנייה!_480p",
        "expected": {
            "media_type": "TV",
            "title": "האח הגדול",
            "season": 15,
            "episode_start": 26,
            "episode_end": None,
            "year": None,
        },
    },
    {
        "input": "480p בוג'ק הורסמן ע1 פ1 נריה סרטים",
        "expected": {
            "media_type": "TV",
            "title": "בוג'ק הורסמן",
            "season": 1,
            "episode_start": 1,
            "episode_end": None,
            "year": None,
        },
    },
    {
        "input": "Total Dhamaal (2019) HDRip 480p קבוצת סרטים",
        "expected": {
            "media_type": "MOVIE",
            "title": "Total Dhamaal",
            "season": None,
            "episode_start": None,
            "episode_end": None,
            "year": 2019,
        },
    },
    {
        # Known issue: uploader prefix remains in the extracted title.
        "input": "480p זירה מדיה האמת ע1 פ2",
        "expected": {
            "media_type": "TV",
            "title": "האמת",
            "season": 1,
            "episode_start": 2,
            "episode_end": None,
            "year": None,
        },
    },
]


def summarize(result):
    episode = result.episode
    return {
        "media_type": result.media_type.value,
        "title": result.title,
        "season": episode.season if episode else None,
        "episode_start": episode.episode_start if episode else None,
        "episode_end": episode.episode_end if episode else None,
        "year": result.year,
    }


class TestClassifierCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = []
        with CORPUS_PATH.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    cls.rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{CORPUS_PATH}: invalid JSON on line {number}"
                    ) from exc

    def test_fixture_integrity(self):
        # Freeze this initial corpus; add new corpora separately.
        self.assertEqual(len(self.rows), 1000)
        ids = [row["callback_data"] for row in self.rows]
        self.assertEqual(len(ids), len(set(ids)))

        for row in self.rows:
            with self.subTest(id=row["callback_data"]):
                self.assertIsInstance(row["result_title"], str)
                self.assertTrue(row["result_title"].strip())

    def test_all_entries_return_valid_results(self):
        counts = Counter()

        for row in self.rows:
            title = row["result_title"]
            with self.subTest(input=title):
                # These are SearchGram titles, not verified filenames.
                result = classify_media(filename=title, caption=None)
                self.assertIsInstance(result.media_type, MediaType)
                counts[result.media_type.value] += 1

                if result.media_type != MediaType.UNKNOWN:
                    self.assertIsInstance(result.title, str)
                    self.assertTrue(result.title.strip())

                if result.media_type == MediaType.TV:
                    self.assertIsNotNone(result.episode)
                    self.assertIsNone(result.movie)

                if result.media_type == MediaType.MOVIE:
                    self.assertIsNotNone(result.movie)
                    self.assertIsNone(result.episode)
                    self.assertEqual(result.title, result.movie.title)
                    self.assertEqual(result.year, result.movie.year)

                if result.episode is not None:
                    episode = result.episode
                    self.assertGreaterEqual(episode.episode_start, 0)
                    if episode.season is not None:
                        self.assertGreaterEqual(episode.season, 0)
                    if episode.episode_end is not None:
                        self.assertGreaterEqual(
                            episode.episode_end, episode.episode_start
                        )

                for match in (result.episode, result.movie):
                    if match is not None:
                        self.assertGreaterEqual(match.confidence, 0)
                        self.assertLessEqual(match.confidence, 1)

        print("\nCorpus predictions (not accuracy):", dict(counts))

    def test_expected_classifications(self):
        corpus_titles = {row["result_title"] for row in self.rows}

        for case in CASES:
            with self.subTest(input=case["input"]):
                self.assertIn(case["input"], corpus_titles)
                result = classify_media(
                    filename=case["input"],
                    caption=None,
                )
                self.assertEqual(summarize(result), case["expected"])


if __name__ == "__main__":
    unittest.main()
