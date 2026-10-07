"""
Search SearchGram once for an entire season and validate every result
against TMDb season metadata before trusting it.

SearchGram search is substring-based ("ע6" also returns unrelated matches,
"פ2" also returns "פ20"/"פ21"), so every result's filename is re-parsed
with the shared classifier and only kept if it resolves to a season/episode
pair that TMDb says belongs to this season.

Usage:
    python search_candidates.py "Show Name" 1 --base-url http://10.0.0.13:8788
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmdb_season import TMDbAmbiguousError, TMDbError, build_season_metadata  # noqa: E402


def _add_shared_path() -> None:
    candidates = (
        # Running directly from the smarthome repository.
        Path(__file__).resolve().parents[2] / "shared",
        # Running inside Docker.
        Path("/shared"),
    )

    for candidate in candidates:
        if candidate.exists():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            return

    raise RuntimeError("Unable to locate shared media_common package")


_add_shared_path()

from media_common.classifier import MediaType, classify_media  # noqa: E402

DEFAULT_BASE_URL = "http://10.0.0.13:8788"
PAGE_DELAY_SECONDS = 1.0
MAX_PAGES = 8
_HEBREW_RE = re.compile(r"[֐-׿]")


def is_hebrew(text: str) -> bool:
    return bool(_HEBREW_RE.search(text))


def build_season_queries(show_name: str, season: int) -> list[str]:
    """
    Ordered query variants to try; the first one that returns any
    results wins.

    "עX" is a near-meaningless 2-character substring on its own (it
    matched 192 unrelated results / 20 pages for a real show here), so
    the full word "עונה X" goes first and the short form is only a
    fallback for catalogs that never spell it out.
    """
    if is_hebrew(show_name):
        return [
            f"{show_name} עונה {season}",
            f"{show_name} ע{season}",
        ]
    return [f"{show_name} S{season:02d}"]


def post(base_url: str, endpoint: str, payload: dict) -> dict:
    request = Request(
        base_url.rstrip("/") + endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def next_callback(page: dict, query: str) -> str | None:
    expected = f"search#{query}#{page['page'] + 1}"
    for button in page.get("navigation", []):
        if button.get("callback_data") == expected:
            return expected
    return None


def fetch_all_results(
    base_url: str,
    query: str,
    max_pages: int = MAX_PAGES,
) -> tuple[list[dict], bool]:
    """
    Search once, then page through results via search#navigate.

    Stops after `max_pages` so an overly broad query (lots of noise,
    many pages) can't turn into a multi-minute scan; returns whether
    it had to stop early.
    """
    items: list[dict] = []
    seen_callbacks: set[str] = set()

    page = post(base_url, "/api/search", {"query": query})
    pages_fetched = 1
    truncated = False

    while True:
        for item in page.get("items", []):
            key = item["callback_data"]
            if key not in seen_callbacks:
                seen_callbacks.add(key)
                items.append(item)

        if page.get("page") == page.get("total_pages"):
            break

        if pages_fetched >= max_pages:
            truncated = True
            break

        callback = next_callback(page, query)
        if callback is None:
            break

        time.sleep(PAGE_DELAY_SECONDS)
        page = post(
            base_url,
            "/api/search/navigate",
            {
                "message_id": page["message_id"],
                "callback_data": callback,
                "query": query,
            },
        )
        pages_fetched += 1

    return items, truncated


def normalize_for_matching(value: str) -> str:
    value = re.sub(r"[_:.\-]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


_SEASON_MARKER_RE = r"(?:עונה\b|ע\d|S\d|Season\b)"


def title_matches_show(title: str, show_name: str) -> bool:
    """
    Reject titles where another word sits between the show name and the
    season marker. "בית הנייר קוריאה עונה 1" is Money Heist: Korea, a
    different show, even though "בית הנייר" is a prefix match and the
    season/episode numbers can coincidentally line up with the original.
    """
    title_norm = normalize_for_matching(title)
    show_norm = normalize_for_matching(show_name)

    pattern = re.compile(
        re.escape(show_norm) + rf"\s*(?={_SEASON_MARKER_RE})",
        re.IGNORECASE,
    )
    return bool(pattern.search(title_norm))


@dataclass
class EpisodeMatch:
    season: int
    episode: int
    title: str
    size: str | None
    callback_data: str


def classify_results(results: list[dict], season: int, show_name: str) -> list[EpisodeMatch]:
    matches = []
    for item in results:
        classification = classify_media(filename=item["title"], caption=None)
        episode = classification.episode

        if (
            classification.media_type != MediaType.TV
            or episode is None
            or episode.season != season
            or episode.episode_end is not None
            or not title_matches_show(item["title"], show_name)
        ):
            continue

        matches.append(
            EpisodeMatch(
                season=season,
                episode=episode.episode_start,
                title=item["title"],
                size=item.get("size"),
                callback_data=item["callback_data"],
            )
        )

    return matches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("show_name", help="Show name to search for")
    parser.add_argument("season", type=int, help="Season number")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--tmdb-id",
        type=int,
        default=None,
        help="Skip TMDb search and use this show id directly (resolve ambiguity manually)",
    )
    args = parser.parse_args()

    tmdb_token = os.getenv("TMDB_API_KEY")
    if not tmdb_token:
        print("TMDB_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    try:
        season_meta = build_season_metadata(args.show_name, args.season, tmdb_token, args.tmdb_id)
    except TMDbAmbiguousError as exc:
        print(str(exc), file=sys.stderr)
        for candidate in exc.candidates:
            print(
                f"  id={candidate['id']} name={candidate.get('name')!r} "
                f"original_name={candidate.get('original_name')!r} "
                f"first_air_date={candidate.get('first_air_date')}",
                file=sys.stderr,
            )
        sys.exit(2)
    except TMDbError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    queries = build_season_queries(args.show_name, args.season)
    all_episode_numbers = {episode.episode for episode in season_meta.episodes}

    # Different uploaders use different conventions (one show matches best
    # on "עונה X", another only on the short "עX"), so a narrower variant
    # is tried first and a broader fallback variant is only queried if
    # episodes are still missing afterwards. The per-query page cap bounds
    # the cost of a broad variant that turns out to be mostly noise
    # (e.g. "עX" alone can match hundreds of unrelated results).
    raw_results: list[dict] = []
    seen_callbacks: set[str] = set()
    truncated = False
    successful_queries: list[str] = []
    matches: list[EpisodeMatch] = []

    for query in queries:
        try:
            page_results, page_truncated = fetch_all_results(args.base_url, query)
        except RuntimeError as exc:
            print(f"Search failed for {query!r}: {exc}", file=sys.stderr)
            continue

        successful_queries.append(query)
        truncated = truncated or page_truncated

        for item in page_results:
            key = item["callback_data"]
            if key not in seen_callbacks:
                seen_callbacks.add(key)
                raw_results.append(item)

        matches = classify_results(raw_results, args.season, args.show_name)
        matched_episodes = {match.episode for match in matches}

        if all_episode_numbers <= matched_episodes:
            break

    if not successful_queries:
        print("All search query variants failed", file=sys.stderr)
        sys.exit(1)

    by_episode: dict[int, list[EpisodeMatch]] = {}
    for match in matches:
        by_episode.setdefault(match.episode, []).append(match)

    episodes_out = []
    for episode in season_meta.episodes:
        candidates = by_episode.get(episode.episode, [])
        episodes_out.append(
            {
                "episode": episode.episode,
                "name": episode.name,
                "candidate_count": len(candidates),
                "candidates": [
                    {
                        "title": candidate.title,
                        "size": candidate.size,
                        "callback_data": candidate.callback_data,
                    }
                    for candidate in candidates
                ],
            }
        )

    output = {
        "show_name": season_meta.show_name,
        "season": season_meta.season,
        "queries_attempted": queries,
        "queries_successful": successful_queries,
        "truncated": truncated,
        "tmdb_episode_count": season_meta.episode_count,
        "raw_result_count": len(raw_results),
        "episodes": episodes_out,
        "missing_episodes": [
            episode["episode"] for episode in episodes_out if episode["candidate_count"] == 0
        ],
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
