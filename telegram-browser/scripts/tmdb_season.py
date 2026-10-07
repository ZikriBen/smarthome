"""
Fetch TMDb season metadata and produce download candidates.

Usage:
    python tmdb_season.py "Show Name" 1
"""

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

TMDB_BASE_URL = "https://api.themoviedb.org/3"


class TMDbError(RuntimeError):
    pass


class TMDbAmbiguousError(TMDbError):
    """Raised when a show can't be auto-resolved; carries the candidates."""

    def __init__(self, message: str, candidates: list[dict]):
        super().__init__(message)
        self.candidates = candidates


@dataclass
class EpisodeCandidate:
    season: int
    episode: int
    name: str
    air_date: str | None
    runtime: int | None


@dataclass
class SeasonMetadata:
    show_id: int
    show_name: str
    season: int
    episode_count: int
    episodes: list[EpisodeCandidate]

    def to_dict(self) -> dict:
        return {
            "show_id": self.show_id,
            "show_name": self.show_name,
            "season": self.season,
            "episode_count": self.episode_count,
            "episodes": [asdict(episode) for episode in self.episodes],
        }


def _get(path: str, token: str, params: dict | None = None) -> dict:
    # v3 API key (32 hex chars) goes as a query param; v4 read access
    # tokens (long JWTs) go as a Bearer header.
    params = dict(params or {})
    headers = {"Accept": "application/json"}

    if len(token) > 40:
        headers["Authorization"] = f"Bearer {token}"
    else:
        params["api_key"] = token

    url = f"{TMDB_BASE_URL}{path}"
    if params:
        url += f"?{urlencode(params)}"

    request = Request(url, headers=headers)

    try:
        with urlopen(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise TMDbError(f"TMDb HTTP {exc.code} for {path}: {detail}") from exc


def _normalize_title(value: str) -> str:
    value = value.strip()
    # Unify apostrophe/geresh variants so "ג'יני" and "ג׳יני" compare equal.
    return value.replace("’", "'").replace("׳", "'")


def find_show(name: str, token: str) -> dict:
    """
    Search TMDb for a TV show, requiring an exact match (in Hebrew or the
    original language) against what was typed rather than trusting TMDb's
    cross-language relevance ranking.

    TMDb's default ranking can put a related-but-wrong show first: the
    Hebrew title for "Game of Thrones" returned "House of the Dragon" as
    the top hit when picking the first result blindly. Requesting
    language=he-IL and requiring an exact match on the localized `name`
    or the `original_name` avoids that.
    """
    result = _get("/search/tv", token, {"query": name, "language": "he-IL"})
    candidates = result.get("results") or []

    if not candidates:
        raise TMDbError(f"No TMDb TV show found for {name!r}")

    query_norm = _normalize_title(name)
    exact_matches = [
        candidate
        for candidate in candidates
        if query_norm
        in (
            _normalize_title(candidate.get("name") or ""),
            _normalize_title(candidate.get("original_name") or ""),
        )
    ]

    if len(exact_matches) == 1:
        return exact_matches[0]

    if len(exact_matches) > 1:
        raise TMDbAmbiguousError(
            f"Multiple TMDb shows exactly match {name!r}; pass --tmdb-id",
            candidates=exact_matches,
        )

    raise TMDbAmbiguousError(
        f"No exact TMDb title match for {name!r}; pass --tmdb-id "
        "or try a different title. Closest results",
        candidates=candidates[:5],
    )


def get_season(show_id: int, season_number: int, token: str) -> dict:
    """Fetch TMDb season details, including the episode list."""
    try:
        return _get(f"/tv/{show_id}/season/{season_number}", token)
    except TMDbError as exc:
        raise TMDbError(
            f"Season {season_number} not found for show id {show_id}: {exc}"
        ) from exc


def build_season_metadata(
    show_name: str,
    season_number: int,
    token: str,
    show_id: int | None = None,
) -> SeasonMetadata:
    show = {"id": show_id, "name": show_name} if show_id else find_show(show_name, token)
    season = get_season(show["id"], season_number, token)

    episodes = [
        EpisodeCandidate(
            season=season_number,
            episode=episode["episode_number"],
            name=episode.get("name") or "",
            air_date=episode.get("air_date"),
            runtime=episode.get("runtime"),
        )
        for episode in season.get("episodes", [])
    ]

    return SeasonMetadata(
        show_id=show["id"],
        show_name=show.get("name") or show_name,
        season=season_number,
        episode_count=len(episodes),
        episodes=episodes,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("show_name", help="Show name to search for on TMDb")
    parser.add_argument("season", type=int, help="Season number")
    parser.add_argument(
        "--tmdb-id",
        type=int,
        default=None,
        help="Skip TMDb search and use this show id directly (resolve ambiguity manually)",
    )
    args = parser.parse_args()

    token = os.getenv("TMDB_API_KEY")
    if not token:
        print("TMDB_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    try:
        metadata = build_season_metadata(args.show_name, args.season, token, args.tmdb_id)
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

    print(json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
