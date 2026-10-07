import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

from dotenv import load_dotenv
from media_common.classifier import (
    MediaType,
    classify_media,
)
from telethon import TelegramClient
from telethon.tl.types import (
    DocumentAttributeFilename,
)

load_dotenv()


DETAILS_TITLE_RE = re.compile(
    r"""
    (?P<title>.+?)
    \s*-\s*
    \((?P<year>19\d{2}|20\d{2})\)
    \s*-\s*
    (?P<english>[^\n]+)
    """,
    re.VERBOSE,
)

FILE_TITLE_RE = re.compile(
    r"""
    ^(?P<title>.+?)
    \s*
    \((?P<year>19\d{2}|20\d{2})\)
    """,
    re.VERBOSE,
)

IMDB_RE = re.compile(
    r"דירוג\s+IMDB:\s*(?P<rating>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

GENRES_RE = re.compile(
    r"ז'אנר:\s*(?P<genres>[^\n]+)",
    re.IGNORECASE,
)

QUALITY_RE = re.compile(
    r"""
    (?P<quality>
        2160P
        |
        1080P
        |
        900P
        |
        720P\+
        |
        720P
        |
        576P
        |
        570P
        |
        540P
        |
        480P
        |
        430P
        |
        360P
        |
        4K
        |
        FHD
        |
        HD
        |
        WEBRIP
        |
        HDRIP
        |
        DVDRIP
        |
        HDTV
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


MAX_STRUCTURAL_VARIANTS = 8
FALLBACK_WINDOW = 8


@dataclass
class MediaVariant:
    message_id: int
    quality: str | None
    filename: str | None
    mime_type: str | None
    size: int | None


@dataclass
class MediaItem:
    title: str
    year: int
    english_title: str | None
    imdb_rating: float | None
    genres: list[str]
    description: str | None

    poster_message_id: int | None
    details_message_id: int

    variants: list[MediaVariant]

    saved: bool = False


@dataclass
class CatalogPage:
    items: list[MediaItem]
    next_cursor: int | None
    has_next: bool


def normalize_title(
    value: str,
) -> str:
    value = value.replace(
        "_",
        " ",
    )

    value = value.replace(
        ".",
        " ",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip(
        " -"
    )


def normalize_comparison_title(
    value: str,
) -> str:
    value = normalize_title(
        value
    ).casefold()

    value = re.sub(
        r"""[,:;!?'"״׳()\[\]{}]""",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def split_alias_titles(
    value: str,
) -> list[str]:
    result = []

    for part in value.split(
        "|"
    ):
        normalized = (
            normalize_comparison_title(
                part
            )
        )

        if normalized:
            result.append(
                normalized
            )

    return result


def simple_titles_compatible(
    details: str,
    file: str,
) -> bool:
    if details == file:
        return True

    # Example:
    # סטפ אפ
    # סטפ אפ 1
    if file == f"{details} 1":
        return True

    if details == f"{file} 1":
        return True

    # Conservative Hebrew definite article support:
    #
    # דרקון
    # הדרקון
    if (
        file.startswith("ה")
        and file[1:] == details
    ):
        return True

    if (
        details.startswith("ה")
        and details[1:] == file
    ):
        return True

    return False


def titles_compatible(
    details_title: str,
    file_title: str,
) -> bool:
    details_aliases = (
        split_alias_titles(
            details_title
        )
    )

    file_aliases = (
        split_alias_titles(
            file_title
        )
    )

    for details in details_aliases:
        for file in file_aliases:
            if simple_titles_compatible(
                details,
                file,
            ):
                return True

    return False


def same_media(
    *,
    title_a: str,
    year_a: int,
    title_b: str,
    year_b: int,
) -> bool:
    if year_a != year_b:
        return False

    return titles_compatible(
        title_a,
        title_b,
    )


def extract_filename(
    message,
) -> str | None:
    document = getattr(
        message,
        "document",
        None,
    )

    if document is None:
        return None

    for attribute in document.attributes:
        if isinstance(
            attribute,
            DocumentAttributeFilename,
        ):
            return (
                attribute.file_name
            )

    return None


def extract_file_size(
    message,
) -> int | None:
    document = getattr(
        message,
        "document",
        None,
    )

    if document is None:
        return None

    return getattr(
        document,
        "size",
        None,
    )


def extract_mime_type(
    message,
) -> str | None:
    document = getattr(
        message,
        "document",
        None,
    )

    if document is None:
        return None

    return getattr(
        document,
        "mime_type",
        None,
    )


def is_photo(
    message,
) -> bool:
    return (
        getattr(
            message,
            "photo",
            None,
        )
        is not None
    )


def is_video_document(
    message,
) -> bool:
    document = getattr(
        message,
        "document",
        None,
    )

    if document is None:
        return False

    mime_type = getattr(
        document,
        "mime_type",
        "",
    )

    return mime_type.startswith(
        "video/"
    )


def parse_details_message(
    message,
) -> dict[str, Any] | None:
    text = (
        message.message or ""
    )

    match = (
        DETAILS_TITLE_RE.search(
            text
        )
    )

    if not match:
        return None

    title = normalize_title(
        match.group(
            "title"
        )
    )

    year = int(
        match.group(
            "year"
        )
    )

    english_title = (
        match.group(
            "english"
        )
        .strip()
        .strip("- ")
    )

    imdb_rating = None

    imdb_match = (
        IMDB_RE.search(
            text
        )
    )

    if imdb_match:
        imdb_rating = float(
            imdb_match.group(
                "rating"
            )
        )

    genres: list[str] = []

    genres_match = (
        GENRES_RE.search(
            text
        )
    )

    if genres_match:
        raw = genres_match.group(
            "genres"
        )

        genres = [
            item.strip()
            .lstrip("#")
            .rstrip(",")
            .replace(
                "_",
                " ",
            )
            for item in raw.split(
                ","
            )
            if item.strip()
        ]

    description = None

    if "תקציר:" in text:
        description_part = (
            text.split(
                "תקציר:",
                1,
            )[1]
        )

        description_part = (
            description_part
            .replace(
                "👇😎",
                "",
            )
            .strip()
        )

        for marker in (
            "\n\n☑️",
            "\n☑️",
            "\n\nלולו סרטים",
        ):
            if marker in description_part:
                description_part = (
                    description_part
                    .split(
                        marker,
                        1,
                    )[0]
                )

        description = (
            description_part.strip()
            or None
        )

    return {
        "title":
            title,

        "year":
            year,

        "english_title":
            english_title or None,

        "imdb_rating":
            imdb_rating,

        "genres":
            genres,

        "description":
            description,
    }


def parse_file_identity(
    message,
) -> tuple[str, int] | None:
    text = (
        message.message or ""
    )

    first_line = (
        text.splitlines()[0]
        if text
        else ""
    )

    match = (
        FILE_TITLE_RE.search(
            first_line
        )
    )

    if match:
        return (
            normalize_title(
                match.group(
                    "title"
                )
            ),
            int(
                match.group(
                    "year"
                )
            ),
        )

    filename = (
        extract_filename(
            message
        )
    )

    classification = (
        classify_media(
            filename=filename,
            caption=(
                text or None
            ),
        )
    )

    if (
        classification.media_type
        == MediaType.MOVIE
        and classification.title
        and classification.year
    ):
        return (
            classification.title,
            classification.year,
        )

    return None


def extract_quality(
    message,
) -> str | None:
    sources = (
        message.message or "",
        extract_filename(
            message
        )
        or "",
    )

    for source in sources:
        match = (
            QUALITY_RE.search(
                source
            )
        )

        if not match:
            continue

        quality = (
            match.group(
                "quality"
            )
            .upper()
        )

        aliases = {
            "FHD":
                "1080p",

            "HD":
                "720p",

            "4K":
                "2160p",

            "WEBRIP":
                "WEBRip",

            "HDRIP":
                "HDRip",

            "DVDRIP":
                "DVDRip",

            "HDTV":
                "HDTV",
        }

        if quality in aliases:
            return aliases[
                quality
            ]

        return (
            quality
            .replace(
                "P",
                "p",
            )
        )

    return None


def quality_rank(
    quality: str | None,
) -> int:
    ranks = {
        "2160p": 100,
        "1080p": 90,
        "900p": 80,
        "720p+": 75,
        "720p": 70,
        "576p": 60,
        "570p": 59,
        "540p": 55,
        "480p": 50,
        "430p": 45,
        "360p": 40,
        "HDTV": 30,
        "WEBRip": 25,
        "HDRip": 20,
        "DVDRip": 10,
    }

    return ranks.get(
        quality or "",
        0,
    )


def variant_from_message(
    message,
) -> MediaVariant:
    return MediaVariant(
        message_id=(
            message.id
        ),
        quality=(
            extract_quality(
                message
            )
        ),
        filename=(
            extract_filename(
                message
            )
        ),
        mime_type=(
            extract_mime_type(
                message
            )
        ),
        size=(
            extract_file_size(
                message
            )
        ),
    )


def collect_structural_variants(
    messages,
    details_index: int,
) -> list[MediaVariant]:
    """
    Lulu's modern movie-post structure is normally:

        video
        video
        [optional additional qualities / CD parts]
        DETAILS
        poster

    This adjacency is much stronger evidence than filename
    title matching.

    We therefore attach the contiguous block of video messages
    immediately preceding the details message.

    We deliberately stop at the first non-video message. This
    prevents us from walking backwards into the previous movie.
    """

    variants: list[
        MediaVariant
    ] = []

    index = (
        details_index - 1
    )

    while (
        index >= 0
        and len(variants)
        < MAX_STRUCTURAL_VARIANTS
    ):
        candidate = (
            messages[index]
        )

        if not is_video_document(
            candidate
        ):
            break

        variants.append(
            variant_from_message(
                candidate
            )
        )

        index -= 1

    variants.reverse()

    return variants


def collect_fallback_variants(
    messages,
    details_index: int,
    details: dict[str, Any],
) -> list[MediaVariant]:
    """
    Conservative fallback for channel layouts that don't place
    video messages immediately before the details post.

    Unlike structural association, this requires title + year
    compatibility.
    """

    start = max(
        0,
        details_index
        - FALLBACK_WINDOW,
    )

    end = min(
        len(messages),
        details_index
        + FALLBACK_WINDOW
        + 1,
    )

    variants: list[
        MediaVariant
    ] = []

    for candidate in messages[
        start:end
    ]:
        if not is_video_document(
            candidate
        ):
            continue

        identity = (
            parse_file_identity(
                candidate
            )
        )

        if identity is None:
            continue

        file_title, file_year = (
            identity
        )

        if not same_media(
            title_a=(
                details["title"]
            ),
            year_a=(
                details["year"]
            ),
            title_b=(
                file_title
            ),
            year_b=(
                file_year
            ),
        ):
            continue

        variants.append(
            variant_from_message(
                candidate
            )
        )

    return variants


def find_poster_message_id(
    messages,
    details_index: int,
) -> int | None:
    """
    Prefer a photo immediately after the details post.

    Typical structure:

        DETAILS
        photo

    If that isn't present, fall back to the closest nearby photo.
    """

    for offset in range(
        1,
        4,
    ):
        index = (
            details_index
            + offset
        )

        if index >= len(
            messages
        ):
            break

        candidate = (
            messages[index]
        )

        if is_photo(
            candidate
        ):
            return candidate.id

        # If we've already reached another video block,
        # don't cross into the next movie.
        if is_video_document(
            candidate
        ):
            break

        # Another details message means we've definitely
        # moved into another item.
        if parse_details_message(
            candidate
        ):
            break

    start = max(
        0,
        details_index
        - FALLBACK_WINDOW,
    )

    end = min(
        len(messages),
        details_index
        + FALLBACK_WINDOW
        + 1,
    )

    closest_id = None
    closest_distance = None

    details_message = (
        messages[
            details_index
        ]
    )

    for candidate in messages[
        start:end
    ]:
        if not is_photo(
            candidate
        ):
            continue

        distance = abs(
            candidate.id
            - details_message.id
        )

        if (
            closest_distance is None
            or distance
            < closest_distance
        ):
            closest_id = (
                candidate.id
            )

            closest_distance = (
                distance
            )

    return closest_id


def deduplicate_variants(
    variants: list[
        MediaVariant
    ],
) -> list[MediaVariant]:
    unique = {
        variant.message_id:
            variant
        for variant in variants
    }

    result = list(
        unique.values()
    )

    result.sort(
        key=lambda item: (
            quality_rank(
                item.quality
            ),
            item.message_id,
        ),
        reverse=True,
    )

    return result


def build_catalog_from_messages(
    messages,
) -> list[MediaItem]:
    messages = list(
        messages
    )

    # Parsing works oldest -> newest.
    messages.sort(
        key=lambda message:
            message.id
    )

    items: list[
        MediaItem
    ] = []

    for (
        index,
        message,
    ) in enumerate(
        messages
    ):
        details = (
            parse_details_message(
                message
            )
        )

        if not details:
            continue

        structural_variants = (
            collect_structural_variants(
                messages,
                index,
            )
        )

        if structural_variants:
            variants = (
                structural_variants
            )

        else:
            variants = (
                collect_fallback_variants(
                    messages,
                    index,
                    details,
                )
            )

        variants = (
            deduplicate_variants(
                variants
            )
        )

        poster_message_id = (
            find_poster_message_id(
                messages,
                index,
            )
        )

        items.append(
            MediaItem(
                title=(
                    details[
                        "title"
                    ]
                ),

                year=(
                    details[
                        "year"
                    ]
                ),

                english_title=(
                    details[
                        "english_title"
                    ]
                ),

                imdb_rating=(
                    details[
                        "imdb_rating"
                    ]
                ),

                genres=(
                    details[
                        "genres"
                    ]
                ),

                description=(
                    details[
                        "description"
                    ]
                ),

                poster_message_id=(
                    poster_message_id
                ),

                details_message_id=(
                    message.id
                ),

                variants=(
                    variants
                ),
            )
        )

    items.sort(
        key=lambda item:
            item.details_message_id,
        reverse=True,
    )

    return items


async def build_catalog(
    client: TelegramClient,
    chat_id: int,
    *,
    limit: int,
) -> list[MediaItem]:
    messages = [
        message
        async for message
        in client.iter_messages(
            chat_id,
            limit=limit,
        )
    ]

    return (
        build_catalog_from_messages(
            messages
        )
    )


async def fetch_catalog_page(
    client: TelegramClient,
    chat_id: int,
    *,
    cursor: int | None,
    page_size: int,
    batch_size: int = 120,
    max_batches: int = 10,
) -> CatalogPage:
    collected_messages = []

    max_id = (
        cursor
        if cursor is not None
        else 0
    )

    reached_end = False

    items: list[
        MediaItem
    ] = []

    for _ in range(
        max_batches
    ):
        batch = [
            message
            async for message
            in client.iter_messages(
                chat_id,
                limit=batch_size,
                max_id=max_id,
            )
        ]

        if not batch:
            reached_end = True
            break

        collected_messages.extend(
            batch
        )

        oldest_id = min(
            message.id
            for message in batch
        )

        max_id = oldest_id

        items = (
            build_catalog_from_messages(
                collected_messages
            )
        )

        if len(
            items
        ) >= (
            page_size + 1
        ):
            break

        if len(
            batch
        ) < batch_size:
            reached_end = True
            break

    page_items = items[
        :page_size
    ]

    has_extra_item = (
        len(items)
        > page_size
    )

    has_next = (
        has_extra_item
        or not reached_end
    )

    next_cursor = None

    if (
        has_next
        and page_items
    ):
        next_cursor = (
            page_items[-1]
            .details_message_id
        )

    return CatalogPage(
        items=page_items,
        next_cursor=next_cursor,
        has_next=has_next,
    )


async def main() -> None:
    api_id = int(
        os.environ[
            "TELEGRAM_API_ID"
        ]
    )

    api_hash = (
        os.environ[
            "TELEGRAM_API_HASH"
        ]
    )

    chat_id = int(
        os.environ[
            "TELEGRAM_SOURCE_CHAT_ID"
        ]
    )

    session = os.getenv(
        "TELEGRAM_SESSION",
        "/data/telegram-browser",
    )

    fetch_limit = int(
        os.getenv(
            "CATALOG_FETCH_LIMIT",
            "200",
        )
    )

    output_limit = int(
        os.getenv(
            "CATALOG_OUTPUT_LIMIT",
            "10",
        )
    )

    client = TelegramClient(
        session,
        api_id,
        api_hash,
    )

    await client.start()

    items = await build_catalog(
        client,
        chat_id,
        limit=fetch_limit,
    )

    print(
        json.dumps(
            [
                asdict(item)
                for item in items[
                    :output_limit
                ]
            ],
            ensure_ascii=False,
            indent=2,
        )
    )

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(
        main()
    )
