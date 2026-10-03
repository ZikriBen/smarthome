"""
Repeatable corpus classification report.

Compares classify_media() output between two classifier.py source files
(default: a round-1 backup vs the current working copy) over the corpus
fixture, and flags rows worth human review.

These are classifier PREDICTIONS over SearchGram result titles, not
verified ground truth and not accuracy scores.

Usage:
    python3 scripts/corpus_report.py \\
        --before ../shared/media_common/classifier.py.round2-<ts>.bak \\
        --after ../shared/media_common/classifier.py \\
        --corpus tests/fixtures/corpus-480p.jsonl \\
        --out /tmp/corpus_report.jsonl
"""

import argparse
import importlib.util
import json
import sys
from collections import Counter
from importlib.machinery import SourceFileLoader
from pathlib import Path


def load_classifier(path: Path):
    loader = SourceFileLoader(f"classifier_{path.stat().st_mtime_ns}", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def summarize(module, title: str) -> dict:
    result = module.classify_media(filename=title, caption=None)
    match = result.episode or result.movie
    return {
        "media_type": result.media_type.value,
        "title": result.title,
        "season": result.episode.season if result.episode else None,
        "episode_start": result.episode.episode_start if result.episode else None,
        "episode_end": result.episode.episode_end if result.episode else None,
        "year": result.year,
        "confidence": match.confidence if match else None,
        "pattern": getattr(match, "pattern", None),
    }


def review_flags(summary: dict) -> list[str]:
    flags = []

    if summary["media_type"] == "UNKNOWN":
        flags.append("unknown")

    if summary["media_type"] == "MOVIE" and summary["year"] is None:
        flags.append("movie_no_year")

    if summary["pattern"] and summary["pattern"].endswith("_assumed_season_1"):
        flags.append("assumed_season")

    if summary["confidence"] is not None and summary["confidence"] < 0.90:
        flags.append("low_confidence")

    title = summary["title"]
    if title:
        letters = sum(ch.isalpha() for ch in title)
        if letters < 4:
            flags.append("short_title")
    elif summary["media_type"] != "UNKNOWN":
        flags.append("missing_title")

    return flags


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    before_mod = load_classifier(args.before)
    after_mod = load_classifier(args.after)

    rows = []
    with args.corpus.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))

    before_counts = Counter()
    after_counts = Counter()
    changed = []
    review = []

    with args.out.open("w", encoding="utf-8") as out:
        for row in rows:
            title = row["result_title"]
            before = summarize(before_mod, title)
            after = summarize(after_mod, title)

            before_counts[before["media_type"]] += 1
            after_counts[after["media_type"]] += 1

            is_changed = before != after
            flags = review_flags(after)

            record = {
                "input": title,
                "callback_data": row.get("callback_data"),
                "before": before,
                "after": after,
                "changed": is_changed,
                "review_flags": flags,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")

            if is_changed:
                changed.append(record)
            if flags:
                review.append(record)

    print(f"Rows: {len(rows)}", file=sys.stderr)
    print(f"Before counts: {dict(before_counts)}", file=sys.stderr)
    print(f"After counts:  {dict(after_counts)}", file=sys.stderr)
    print(f"Changed: {len(changed)}", file=sys.stderr)
    print(f"Review-flagged (after): {len(review)}", file=sys.stderr)
    print(f"Full report written to: {args.out}", file=sys.stderr)

    flag_counts = Counter(f for r in review for f in r["review_flags"])
    print(f"Review flag breakdown: {dict(flag_counts)}", file=sys.stderr)


if __name__ == "__main__":
    main()
