import re
from dataclasses import dataclass, replace
from enum import StrEnum


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
ASSUMED_SEASON_CONFIDENCE = 0.85

# Downloadable content is assumed to be a movie unless it carries explicit
# season/episode evidence. This mirrors ASSUMED_SEASON_CONFIDENCE's
# convention: equal to the default auto_movie_threshold so it auto-classifies
# unless a caller passes a stricter threshold.
ASSUMED_MOVIE_CONFIDENCE = 0.90


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
                \s*[-+]\s*(?:E)?(?P<episode_end_dash>\d{1,3})
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
        name="hebrew_short_episode_only",
        regex=re.compile(
            rf"(?<![^\W_])פ['׳\"]?{SEPARATOR}"
            r"(?P<episode_start>\d{1,3})(?![^\W_])",
        ),
        confidence=ASSUMED_SEASON_CONFIDENCE,
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
        confidence=ASSUMED_SEASON_CONFIDENCE,
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
        confidence=ASSUMED_SEASON_CONFIDENCE,
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
    r"זירה[ _.-]+מדיה",
    r"נריה[ _.-]+סרטים",
    r"(?:NF[ _.-]+)?נתי[ _.-]+מדיה",
    r"קינג[ _.-]+סרט",
    r"יוסי[ _.-]+סרטים",
    r"כל[ _.-]+הסדרות",
    r"מדיה[ _.-]+VOD",
    r"נ[ _.-]+מדיה",
    r"שלום[ _.-]+מדיה",
)


# Multi-part markers ("חלק 1", "ח1", "CD2") deliberately do not imply a TV
# season/episode (agreed policy), but they also should not be silently
# classified as a single standalone movie: tying multiple parts together
# is unsolved deferred work. Exclude them from the yearless movie fallback
# below rather than guess.
MULTIPART_MARKER_RE = re.compile(
    r"""
    חלק[\s._-]*\d
    |
    (?<![א-ת])ח\d
    |
    (?<![A-Za-z])CD\d
    """,
    re.IGNORECASE | re.VERBOSE,
)


RELEASE_NOISE_RE = re.compile(
    rf"""
    (?<![^\W_])
    (?:
        {'|'.join(RELEASE_TOKENS)}
        |
        {'|'.join(HEBREW_RELEASE_TOKENS)}
    )
    (?![^\W_])
    """,
    re.IGNORECASE | re.VERBOSE,
)


SOURCE_PREFIX_RE = re.compile(
    rf"""
    ^\s*
    (?:
        {'|'.join(SOURCE_PREFIXES)}
    )
    (?![^\W_])
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



MEDIA_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".m4v",
    ".wmv",
    ".ts",
    ".webm",
}


def strip_media_extension(
    value: str,
) -> str:
    """
    Remove only a known media extension.

    Do not use Path(value).suffix/.with_suffix() on arbitrary title
    fragments: besides misreading internal punctuation such as "ז.מ"
    as a filename extension, pathlib normalizes path separators (e.g.
    collapsing "https://" to "https:/"), which would corrupt the
    URL/invite-link signature that is_suspicious_title relies on.
    """
    dot_index = value.rfind(".")

    if dot_index == -1:
        return value

    if (
        value[dot_index:].lower()
        in MEDIA_EXTENSIONS
    ):
        return value[:dot_index]

    return value


def trim_leading_metadata_fragments(
    value: str,
) -> str:
    """
    Remove leading groups of isolated one-character metadata
    fragments.

    Example:

        "ז מ כנופיית ברמינגהם"
            ->
        "כנופיית ברמינגהם"

    We require at least TWO consecutive one-character tokens.
    This prevents legitimate titles such as "V Something"
    from losing their first word.
    """
    parts = value.split()

    fragment_count = 0

    for part in parts:
        if (
            len(part) == 1
            and part.isalnum()
        ):
            fragment_count += 1
            continue

        break

    if (
        fragment_count >= 2
        and fragment_count < len(parts)
    ):
        parts = parts[
            fragment_count:
        ]

    return " ".join(parts)



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
    value = normalize_text(value)

    # Strip markdown/URLs BEFORE converting separators: URL_RE relies on
    # intact dots (e.g. "t\.me/"), and converting "." to " " first would
    # truncate the match, leaving a corrupted URL fragment that no longer
    # looks suspicious but also isn't a real title.
    value = clean_markdown(value)

    # Convert filename separators BEFORE doing any other cleanup.
    value = re.sub(
        r"[._]+",
        " ",
        value,
    )

    value = SOURCE_PREFIX_RE.sub(
        "",
        value,
    )

    value = RELEASE_NOISE_RE.sub(
        " ",
        value,
    )
    value = SOURCE_PREFIX_RE.sub("", value)

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

    if letters < 2:
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
    prepared_value = (
        strip_media_extension(value)
        if source == "filename"
        else value
    )

    prepared = normalize_text(
        prepared_value
    )

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

    title = trim_leading_metadata_fragments(
        title
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


def clean_candidate_title(
    *,
    source: str,
    value: str,
) -> str | None:
    """
    Run the shared extension-strip -> normalize -> clean -> trim pipeline
    on a single (source, value) candidate. Returns None for an empty
    result; does not apply is_suspicious_title or multipart filtering,
    since callers use this for different purposes (auto-classification
    vs. an external-lookup query).
    """
    prepared_value = (
        strip_media_extension(value)
        if source == "filename"
        else value
    )

    prepared = normalize_text(
        prepared_value
    )

    title = clean_title_text(prepared)
    title = trim_leading_metadata_fragments(title)

    return title or None


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
        prepared_value = (
            strip_media_extension(value)
            if source == "filename"
            else value
        )

        prepared = normalize_text(
            prepared_value
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

        title_part = prepared[:year_match.start()]
        # Support release metadata + year + title, as well as title + year.
        if not clean_title_text(title_part):
            title_part = prepared[year_match.end():]

        title = clean_title_text(
            title_part
        )

        title = trim_leading_metadata_fragments(
            title
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

    # No year anchor in any source. Downloadable content is assumed to be
    # a movie unless it carries season/episode evidence (classify_media
    # only reaches this function when no episode pattern matched at all),
    # so fall back to a bare cleaned title rather than giving up.
    for source, value in candidates:
        title = clean_candidate_title(
            source=source,
            value=value,
        )

        if not title:
            continue

        if is_suspicious_title(title):
            continue

        if MULTIPART_MARKER_RE.search(title):
            continue

        return MovieMatch(
            title=title,
            year=None,
            confidence=ASSUMED_MOVIE_CONFIDENCE,
            source=source,
        )

    return None


def best_effort_title(
    *,
    filename: str | None,
    caption: str | None,
) -> str | None:
    """
    A cleaned title candidate for sources that classify_media() could not
    confidently classify (UNKNOWN). Unlike the movie/TV extraction paths,
    this applies no suspicion or multipart filtering: it is meant only as
    a query for an external lookup (e.g. TMDb) to confirm or resolve,
    never as a basis for auto-classification by itself.
    """
    for source, value in (
        ("filename", filename),
        ("caption", caption),
    ):
        if not value:
            continue

        title = clean_candidate_title(
            source=source,
            value=value,
        )

        if title:
            return title

    return None


def _unsupported_episode_sequence(value: str) -> bool:
    """Reject episode lists our start/end model cannot represent faithfully."""
    sequence_re = re.compile(
        r"(?<![A-Za-z0-9])S\d{1,2}[\s._-]*E(?P<first>\d{1,3})"
        r"(?P<tail>(?:(?:[\s._]*E|[\s._]*[-+][\s._]*E?)\d{1,3})+)",
        re.IGNORECASE,
    )
    for match in sequence_re.finditer(value):
        tail = match.group("tail")
        numbers = re.findall(r"\d+", tail)
        if len(numbers) != 1:
            return True
        first, last = int(match.group("first")), int(numbers[0])
        if last < first:
            return True
        # A dash denotes a range; '+' and repeated E denote individual episodes.
        if "-" not in tail and last != first + 1:
            return True
    return False


def classify_media(
    *,
    filename: str | None,
    caption: str | None,
    auto_tv_threshold: float = 0.85,
    auto_movie_threshold: float = 0.90,
) -> ClassificationResult:
    sources = []
    if filename:
        sources.append(("filename", filename))
    if caption:
        sources.append(("caption", clean_markdown(caption)))

    if any(_unsupported_episode_sequence(value) for _, value in sources):
        return ClassificationResult(media_type=MediaType.UNKNOWN)

    matches = []
    for source, value in sources:
        match = _find_best_episode_match(value, source=source)
        if match:
            matches.append(match)

    if matches:
        best = max(matches, key=lambda item: (
            item.confidence, item.source == "filename",
        ))
        # Conflicting explicit information must not silently move the wrong file.
        if len(matches) == 2:
            left, right = matches
            season_conflict = (
                left.season is not None and right.season is not None
                and left.season != right.season
            )
            episode_conflict = (
                (left.episode_start, left.episode_end)
                != (right.episode_start, right.episode_end)
            )
            if season_conflict or episode_conflict:
                return ClassificationResult(
                    media_type=MediaType.UNKNOWN, episode=best,
                )

        if best.confidence >= auto_tv_threshold:
            # Keep title and episode evidence from the same source when possible.
            ordered = sorted(sources, key=lambda item: item[0] != best.source)
            title = None
            for source, value in ordered:
                title = extract_title_from_source(value=value, source=source)
                if title:
                    break
            if title:
                if best.season is None:
                    best = replace(
                        best, season=1,
                        pattern=best.pattern + "_assumed_season_1",
                    )
                return ClassificationResult(
                    media_type=MediaType.TV, episode=best, title=title,
                )
        # A year in a rejected episode must not turn it into a movie.
        return ClassificationResult(media_type=MediaType.UNKNOWN, episode=best)

    movie = extract_movie_match(filename=filename, caption=caption)
    if movie is not None and movie.confidence >= auto_movie_threshold:
        return ClassificationResult(
            media_type=MediaType.MOVIE, movie=movie,
            title=movie.title, year=movie.year,
        )
    return ClassificationResult(media_type=MediaType.UNKNOWN)

