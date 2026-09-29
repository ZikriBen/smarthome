import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class MediaType(StrEnum):
    TV = "TV"
    MOVIE = "MOVIE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EpisodeMatch:
    season: int | None
    episode_start: int
    episode_end: int | None
    confidence: float
    pattern: str
    matched_text: str
    source: str


@dataclass(frozen=True)
class MovieMatch:
    title: str
    year: int | None
    confidence: float
    source: str


@dataclass(frozen=True)
class ClassificationResult:
    media_type: MediaType
    episode: EpisodeMatch | None = None
    movie: MovieMatch | None = None
    title: str | None = None
    year: int | None = None


@dataclass(frozen=True)
class EpisodePattern:
    name: str
    regex: re.Pattern[str]
    confidence: float


SEPARATOR = r"[\s._-]*"


PATTERNS: tuple[EpisodePattern, ...] = (
    EpisodePattern(
        name="sxe_multi",
        regex=re.compile(
            r"""
            (?<![A-Za-z0-9])
            S(?P<season>\d{1,2})
            [\s._-]*
            E(?P<episode_start>\d{1,3})
            (?:
                [\s._-]*E(?P<episode_end_e>\d{1,3})
                |
                \s*-\s*(?:E)?(?P<episode_end_dash>\d{1,3})
            )
            (?!\d)
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
        confidence=1.0,
    ),
    EpisodePattern(
        name="sxe",
        regex=re.compile(
            rf"""
            (?<![A-Za-z0-9])
            S(?P<season>\d{{1,2}})
            {SEPARATOR}
            E(?P<episode_start>\d{{1,3}})
            (?!\d)
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
        confidence=1.0,
    ),
    EpisodePattern(
        name="x_format",
        regex=re.compile(
            r"""
            (?<!\d)
            (?P<season>\d{1,2})
            \s*[xX]\s*
            (?P<episode_start>\d{1,3})
            (?!\d)
            """,
            re.VERBOSE,
        ),
        confidence=0.95,
    ),
    EpisodePattern(
        name="english_verbose",
        regex=re.compile(
            rf"""
            \bseason
            {SEPARATOR}
            (?P<season>\d{{1,2}})
            {SEPARATOR}
            (?:episode|ep)
            {SEPARATOR}
            (?P<episode_start>\d{{1,3}})
            \b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
        confidence=1.0,
    ),
    EpisodePattern(
        name="hebrew_verbose",
        regex=re.compile(
            rf"""
            עונה
            {SEPARATOR}
            (?P<season>\d{{1,2}})
            {SEPARATOR}
            פרק
            {SEPARATOR}
            (?P<episode_start>\d{{1,3}})
            """,
            re.VERBOSE,
        ),
        confidence=1.0,
    ),
    EpisodePattern(
        name="hebrew_reversed",
        regex=re.compile(
            rf"""
            פרק
            {SEPARATOR}
            (?P<episode_start>\d{{1,3}})
            {SEPARATOR}
            עונה
            {SEPARATOR}
            (?P<season>\d{{1,2}})
            """,
            re.VERBOSE,
        ),
        confidence=1.0,
    ),
    EpisodePattern(
        name="hebrew_short",
        regex=re.compile(
            rf"""
            ע(?:ונה)?
            ['׳"]?
            {SEPARATOR}
            (?P<season>\d{{1,2}})
            {SEPARATOR}
            פ(?:רק)?
            ['׳"]?
            {SEPARATOR}
            (?P<episode_start>\d{{1,3}})
            (?!\d)
            """,
            re.VERBOSE,
        ),
        confidence=0.90,
    ),
    EpisodePattern(
        name="english_episode_only",
        regex=re.compile(
            rf"""
            \b(?:episode|ep)
            {SEPARATOR}
            (?P<episode_start>\d{{1,3}})
            \b
            """,
            re.IGNORECASE | re.VERBOSE,
        ),
        confidence=0.60,
    ),
    EpisodePattern(
        name="hebrew_episode_only",
        regex=re.compile(
            rf"""
            פרק
            {SEPARATOR}
            (?P<episode_start>\d{{1,3}})
            """,
            re.VERBOSE,
        ),
        confidence=0.60,
    ),
)


RELEASE_TOKENS = (
    r"480p",
    r"576p",
    r"720p",
    r"1080p",
    r"1080i",
    r"2160p",
    r"4k",
    r"uhd",
    r"web[-_. ]?dl",
    r"webrip",
    r"bluray",
    r"brrip",
    r"dvdrip",
    r"hdtv",
    r"hdrip",
    r"remux",
    r"x264",
    r"x265",
    r"h\.?264",
    r"h\.?265",
    r"hevc",
    r"av1",
    r"aac",
    r"ac3",
    r"ddp?5\.?1",
    r"10bit",
    r"hdr",
    r"dolby",
    r"atmos",
    r"proper",
    r"repack",
)


HEBREW_RELEASE_TOKENS = (
    r"תרגום[ _.-]*מובנה",
    r"ת[._ -]?מ",
    r"מדובב",
    r"כתוביות",
    r"איכות",
    r"ישראלי",
    r"השימיה",
)


SOURCE_PREFIXES = (
    r"לולו[ _.-]*סרטים",
)


RELEASE_NOISE_RE = re.compile(
    rf"""
    (?:
        {'|'.join(RELEASE_TOKENS)}
        |
        {'|'.join(HEBREW_RELEASE_TOKENS)}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


SOURCE_PREFIX_RE = re.compile(
    rf"""
    ^\s*
    (?:
        {'|'.join(SOURCE_PREFIXES)}
    )
    [\s._:-]*
    """,
    re.IGNORECASE | re.VERBOSE,
)


YEAR_RE = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})(?!\d)"
)


MARKDOWN_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]+)\]\([^)]+\)"
)


URL_RE = re.compile(
    r"https?://\S+|t\.me/\S+",
    re.IGNORECASE,
)


# Important: "_" is deliberately NOT removed here.
# It is a meaningful filename separator.
MARKDOWN_DECORATION_RE = re.compile(
    r"[*`~]+"
)


def normalize_text(value: str) -> str:
    value = value.replace("–", "-")
    value = value.replace("—", "-")
    value = value.replace("־", "-")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def clean_markdown(value: str) -> str:
    value = MARKDOWN_LINK_RE.sub(
        lambda match: match.group("label"),
        value,
    )

    value = URL_RE.sub(
        " ",
        value,
    )

    value = MARKDOWN_DECORATION_RE.sub(
        "",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def clean_title_text(value: str) -> str:
    value = Path(value).stem
    value = normalize_text(value)

    # Convert filename separators BEFORE doing any other cleanup.
    value = re.sub(
        r"[._]+",
        " ",
        value,
    )

    value = clean_markdown(value)

    value = SOURCE_PREFIX_RE.sub(
        "",
        value,
    )

    value = RELEASE_NOISE_RE.sub(
        " ",
        value,
    )

    value = re.sub(
        r"[\[\](){}]",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    # Clean spacing around punctuation.
    value = re.sub(
        r"\s*:\s*",
        ": ",
        value,
    )

    return value.strip(" -_.~")


def is_suspicious_title(value: str) -> bool:
    value = value.strip()

    if not value:
        return True

    letters = sum(
        char.isalpha()
        for char in value
    )

    if letters < 3:
        return True

    # Telegram invite/base64-like garbage:
    # +YSofgZNVx71hNTY0
    compact = value.replace(" ", "")

    if (
        len(compact) >= 12
        and compact.startswith("+")
        and re.fullmatch(
            r"[+A-Za-z0-9/_=-]+",
            compact,
        )
    ):
        return True

    # URL-ish values should never become titles.
    lowered = value.lower()

    if (
        "t.me/" in lowered
        or "http://" in lowered
        or "https://" in lowered
    ):
        return True

    return False


def _find_best_episode_match(
    text: str,
    source: str,
) -> EpisodeMatch | None:
    text = normalize_text(text)

    best: EpisodeMatch | None = None

    for pattern in PATTERNS:
        for match in pattern.regex.finditer(text):
            groups = match.groupdict()

            season_raw = groups.get(
                "season"
            )

            episode_start_raw = groups.get(
                "episode_start"
            )

            episode_end_raw = (
                groups.get("episode_end")
                or groups.get("episode_end_e")
                or groups.get("episode_end_dash")
            )

            if episode_start_raw is None:
                continue

            result = EpisodeMatch(
                season=(
                    int(season_raw)
                    if season_raw is not None
                    else None
                ),
                episode_start=int(
                    episode_start_raw
                ),
                episode_end=(
                    int(episode_end_raw)
                    if episode_end_raw is not None
                    else None
                ),
                confidence=pattern.confidence,
                pattern=pattern.name,
                matched_text=match.group(0),
                source=source,
            )

            if (
                best is None
                or result.confidence
                > best.confidence
            ):
                best = result

    return best


def extract_title_from_source(
    *,
    value: str,
    source: str,
) -> str | None:
    """
    Find the episode notation inside THIS source and take
    everything before it as the candidate title.

    This is intentionally source-specific because the filename
    may contain "ע3 פ21" while the caption contains
    "עונה 3 פרק 21".
    """
    prepared = normalize_text(value)

    if source == "caption":
        prepared = clean_markdown(
            prepared
        )

    episode = _find_best_episode_match(
        prepared,
        source=source,
    )

    if episode is None:
        return None

    matched = episode.matched_text

    idx = prepared.lower().find(
        matched.lower()
    )

    if idx < 0:
        return None

    prefix = prepared[:idx]

    title = clean_title_text(
        prefix
    )

    if not title:
        return None

    if is_suspicious_title(title):
        return None

    return title


def extract_show_title(
    *,
    filename: str | None,
    caption: str | None,
) -> str | None:
    # Filename is preferable because it is generally more compact
    # and less likely to contain Telegram links or channel text.
    if filename:
        title = extract_title_from_source(
            value=filename,
            source="filename",
        )

        if title:
            return title

    if caption:
        title = extract_title_from_source(
            value=caption,
            source="caption",
        )

        if title:
            return title

    return None


def extract_movie_match(
    *,
    filename: str | None,
    caption: str | None,
) -> MovieMatch | None:
    candidates: list[
        tuple[str, str]
    ] = []

    if filename:
        candidates.append(
            ("filename", filename)
        )

    if caption:
        candidates.append(
            ("caption", caption)
        )

    for source, value in candidates:
        prepared = normalize_text(
            Path(value).stem
        )

        if source == "caption":
            prepared = clean_markdown(
                prepared
            )

        # Convert separators before year/title parsing.
        prepared = re.sub(
            r"[._]+",
            " ",
            prepared,
        )

        prepared = SOURCE_PREFIX_RE.sub(
            "",
            prepared,
        )

        year_match = YEAR_RE.search(
            prepared
        )

        if not year_match:
            continue

        year = int(
            year_match.group("year")
        )

        title_part = prepared[
            :year_match.start()
        ]

        title = clean_title_text(
            title_part
        )

        if not title:
            continue

        if is_suspicious_title(title):
            continue

        return MovieMatch(
            title=title,
            year=year,
            confidence=0.95,
            source=source,
        )

    return None


def classify_media(
    *,
    filename: str | None,
    caption: str | None,
    auto_tv_threshold: float = 0.85,
    auto_movie_threshold: float = 0.90,
) -> ClassificationResult:
    episode_matches: list[
        EpisodeMatch
    ] = []

    if filename:
        match = _find_best_episode_match(
            filename,
            source="filename",
        )

        if match:
            episode_matches.append(
                match
            )

    if caption:
        match = _find_best_episode_match(
            clean_markdown(caption),
            source="caption",
        )

        if match:
            episode_matches.append(
                match
            )

    if episode_matches:
        best_episode = max(
            episode_matches,
            key=lambda item: (
                item.confidence,
                item.source
                == "filename",
            ),
        )

        if (
            best_episode.confidence
            >= auto_tv_threshold
        ):
            title = extract_show_title(
                filename=filename,
                caption=caption,
            )

            if title:
                return ClassificationResult(
                    media_type=MediaType.TV,
                    episode=best_episode,
                    movie=None,
                    title=title,
                    year=None,
                )

    movie = extract_movie_match(
        filename=filename,
        caption=caption,
    )

    if (
        movie is not None
        and movie.confidence
        >= auto_movie_threshold
    ):
        return ClassificationResult(
            media_type=MediaType.MOVIE,
            episode=None,
            movie=movie,
            title=movie.title,
            year=movie.year,
        )

    return ClassificationResult(
        media_type=MediaType.UNKNOWN,
        episode=(
            episode_matches[0]
            if episode_matches
            else None
        ),
        movie=None,
        title=None,
        year=None,
    )
