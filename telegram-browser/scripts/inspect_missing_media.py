import asyncio
import json
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient


PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parents[1]
)

load_dotenv(
    PROJECT_ROOT / ".env"
)


TARGETS = [
    "יונבומבר",
    "לסט ג'ורס דו טאליון",
    "שבעה ימים של נקמה",
    "תאומי השלד",
    "התאומים",
    "המנסרים מטקסס: הדור הבא",
    "מוטור סיטי",
    "מוות אכזרי",
    "Aida's Secrets",
    "הולופיקשן",
    "השואה בקולנוע",
    "הסנדק: חלק שני",
    "מקסיקו 86",
    "אבא שלי",
    "רוצח ה-BTK",
]


DATABASE_PATH = os.getenv(
    "CATALOG_DATABASE_PATH",
    "/data/catalog.db",
)

WINDOW = 12


def normalize(
    value: str,
) -> str:
    return (
        value
        .casefold()
        .strip()
    )


def find_matching_movies():
    db = sqlite3.connect(
        DATABASE_PATH
    )

    db.row_factory = sqlite3.Row

    rows = db.execute(
        """
        SELECT
            details_message_id,
            title,
            english_title,
            year,
            imdb_rating,
            genres_json

        FROM movies

        ORDER BY
            details_message_id DESC
        """
    ).fetchall()

    db.close()

    matches = []

    for row in rows:
        searchable = " ".join(
            [
                row["title"] or "",
                row["english_title"] or "",
                str(row["year"] or ""),
            ]
        )

        searchable_normalized = normalize(
            searchable
        )

        matched_targets = [
            target
            for target in TARGETS
            if (
                normalize(target)
                in searchable_normalized
            )
        ]

        if not matched_targets:
            continue

        matches.append(
            {
                "details_message_id":
                    row[
                        "details_message_id"
                    ],

                "title":
                    row["title"],

                "english_title":
                    row[
                        "english_title"
                    ],

                "year":
                    row["year"],

                "matched_targets":
                    matched_targets,
            }
        )

    return matches


async def inspect_movie(
    client: TelegramClient,
    chat_id: int,
    movie: dict,
) -> None:
    details_id = movie[
        "details_message_id"
    ]

    min_id = max(
        0,
        details_id - WINDOW - 1,
    )

    max_id = (
        details_id
        + WINDOW
        + 1
    )

    messages = [
        message
        async for message
        in client.iter_messages(
            chat_id,
            min_id=min_id,
            max_id=max_id,
            reverse=True,
        )
    ]

    print()
    print(
        "#" * 100
    )

    print(
        "MOVIE:",
        movie["title"],
        f"({movie['year']})",
    )

    print(
        "ENGLISH:",
        movie[
            "english_title"
        ],
    )

    print(
        "DETAILS MESSAGE ID:",
        details_id,
    )

    print(
        "MATCHED TARGETS:",
        ", ".join(
            movie[
                "matched_targets"
            ]
        ),
    )

    print(
        "#" * 100
    )

    for message in messages:
        filename = getattr(
            message.file,
            "name",
            None,
        )

        size = getattr(
            message.file,
            "size",
            None,
        )

        mime = getattr(
            message.file,
            "mime_type",
            None,
        )

        marker = (
            " <<< DETAILS"
            if message.id
            == details_id
            else ""
        )

        print()
        print(
            "=" * 90
        )

        print(
            f"ID: {message.id}"
            f"{marker}"
        )

        print(
            "TEXT:"
        )

        print(
            message.message
            or "<empty>"
        )

        print(
            "FILE:",
            filename,
        )

        print(
            "SIZE:",
            size,
        )

        print(
            "MIME:",
            mime,
        )


async def main():
    movies = (
        find_matching_movies()
    )

    if not movies:
        print(
            "No matching movies found "
            "in catalog.db"
        )

        return

    print(
        "Found",
        len(movies),
        "matching indexed movie(s)"
    )

    client = TelegramClient(
        os.environ[
            "TELEGRAM_SESSION"
        ],
        int(
            os.environ[
                "TELEGRAM_API_ID"
            ]
        ),
        os.environ[
            "TELEGRAM_API_HASH"
        ],
    )

    await client.start()

    chat_id = int(
        os.environ[
            "TELEGRAM_SOURCE_CHAT_ID"
        ]
    )

    for movie in movies:
        await inspect_movie(
            client,
            chat_id,
            movie,
        )

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(
        main()
    )
