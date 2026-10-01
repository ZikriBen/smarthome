import json

from app.genres import normalize_genre, normalize_genres
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.catalog import (
    MediaItem,
    MediaVariant,
)


@dataclass
class CatalogResult:
    items: list[MediaItem]
    page: int
    page_size: int
    total: int
    total_pages: int
    has_previous: bool
    has_next: bool


class CatalogDatabase:
    def __init__(
        self,
        path: str,
    ):
        self.path = Path(path)

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.initialize()

    def connect(
        self,
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
        )

        connection.row_factory = (
            sqlite3.Row
        )

        connection.execute(
            "PRAGMA journal_mode=WAL"
        )

        connection.execute(
            "PRAGMA foreign_keys=ON"
        )

        return connection

    def initialize(
        self,
    ) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS movies (
                    details_message_id INTEGER PRIMARY KEY,

                    title TEXT NOT NULL,
                    english_title TEXT,

                    year INTEGER NOT NULL,
                    imdb_rating REAL,

                    genres_json TEXT NOT NULL,
                    description TEXT,

                    poster_message_id INTEGER,

                    created_at TEXT
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    updated_at TEXT
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS variants (
                    message_id INTEGER PRIMARY KEY,

                    details_message_id INTEGER NOT NULL,

                    quality TEXT,
                    filename TEXT,
                    mime_type TEXT,
                    size INTEGER,

                    FOREIGN KEY (
                        details_message_id
                    )
                    REFERENCES movies (
                        details_message_id
                    )
                    ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS
                    idx_movies_year
                ON movies(year);

                CREATE INDEX IF NOT EXISTS
                    idx_movies_rating
                ON movies(imdb_rating);

                CREATE INDEX IF NOT EXISTS
                    idx_movies_details
                ON movies(details_message_id);

                CREATE INDEX IF NOT EXISTS
                    idx_variants_movie
                ON variants(details_message_id);
                """
            )

    def upsert_items(
        self,
        items: list[MediaItem],
    ) -> int:
        if not items:
            return 0

        with self.connect() as db:
            for item in items:
                db.execute(
                    """
                    INSERT INTO movies (
                        details_message_id,
                        title,
                        english_title,
                        year,
                        imdb_rating,
                        genres_json,
                        description,
                        poster_message_id,
                        updated_at
                    )
                    VALUES (
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        CURRENT_TIMESTAMP
                    )

                    ON CONFLICT(
                        details_message_id
                    )
                    DO UPDATE SET
                        title =
                            excluded.title,

                        english_title =
                            excluded.english_title,

                        year =
                            excluded.year,

                        imdb_rating =
                            excluded.imdb_rating,

                        genres_json =
                            excluded.genres_json,

                        description =
                            excluded.description,

                        poster_message_id =
                            excluded.poster_message_id,

                        updated_at =
                            CURRENT_TIMESTAMP
                    """,
                    (
                        item.details_message_id,
                        item.title,
                        item.english_title,
                        item.year,
                        item.imdb_rating,
                        json.dumps(
                            normalize_genres(
                                item.genres
                            ),
                            ensure_ascii=False,
                        ),
                        item.description,
                        item.poster_message_id,
                    ),
                )

                db.execute(
                    """
                    DELETE FROM variants
                    WHERE details_message_id = ?
                    """,
                    (
                        item.details_message_id,
                    ),
                )

                for variant in (
                    item.variants
                ):
                    db.execute(
                        """
                        INSERT OR REPLACE
                        INTO variants (
                            message_id,
                            details_message_id,
                            quality,
                            filename,
                            mime_type,
                            size
                        )
                        VALUES (
                            ?,
                            ?,
                            ?,
                            ?,
                            ?,
                            ?
                        )
                        """,
                        (
                            variant.message_id,
                            item.details_message_id,
                            variant.quality,
                            variant.filename,
                            variant.mime_type,
                            variant.size,
                        ),
                    )

            db.commit()

        return len(items)

    def get_state(
        self,
        key: str,
    ) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT value
                FROM sync_state
                WHERE key = ?
                """,
                (key,),
            ).fetchone()

        if row is None:
            return None

        return str(
            row["value"]
        )

    def set_state(
        self,
        key: str,
        value: str | int | bool,
    ) -> None:
        if isinstance(
            value,
            bool,
        ):
            stored = (
                "1"
                if value
                else "0"
            )
        else:
            stored = str(
                value
            )

        with self.connect() as db:
            db.execute(
                """
                INSERT INTO sync_state (
                    key,
                    value
                )
                VALUES (
                    ?,
                    ?
                )

                ON CONFLICT(key)
                DO UPDATE SET
                    value =
                        excluded.value
                """,
                (
                    key,
                    stored,
                ),
            )

            db.commit()

    def get_state_int(
        self,
        key: str,
    ) -> int | None:
        value = self.get_state(
            key
        )

        if value is None:
            return None

        try:
            return int(
                value
            )
        except ValueError:
            return None

    def get_state_bool(
        self,
        key: str,
        default: bool = False,
    ) -> bool:
        value = self.get_state(
            key
        )

        if value is None:
            return default

        return value in (
            "1",
            "true",
            "True",
        )

    def count_movies(
        self,
    ) -> int:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS count
                FROM movies
                """
            ).fetchone()

        return int(
            row["count"]
        )

    def get_genres(
        self,
    ) -> list[str]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT genres_json
                FROM movies
                """
            ).fetchall()

        genres: set[str] = set()

        for row in rows:
            try:
                values = json.loads(
                    row["genres_json"]
                )
            except (
                json.JSONDecodeError,
                TypeError,
            ):
                continue

            for genre in normalize_genres(
                [
                    str(value)
                    for value in values
                    if value
                ]
            ):
                genres.add(
                    genre
                )

        return sorted(
            genres
        )

    def query_movies(
        self,
        *,
        page: int,
        page_size: int,
        search: str,
        genre: str,
        sort: str,
    ) -> CatalogResult:
        conditions: list[str] = []

        params: list[
            str | int | float
        ] = []

        if search:
            pattern = (
                f"%{search.strip()}%"
            )

            conditions.append(
                """
                (
                    title LIKE ?
                    OR english_title LIKE ?
                    OR CAST(year AS TEXT) LIKE ?
                    OR genres_json LIKE ?
                    OR description LIKE ?
                )
                """
            )

            params.extend(
                [
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                    pattern,
                ]
            )

        if genre:
            genre = normalize_genre(
                genre
            )

            conditions.append(
                """
                genres_json LIKE ?
                """
            )

            params.append(
                f'%"{genre}"%'
            )

        where_sql = ""

        if conditions:
            where_sql = (
                "WHERE "
                + " AND ".join(
                    conditions
                )
            )

        order_sql = {
            "newest":
                """
                details_message_id DESC
                """,

            "year_desc":
                """
                year DESC,
                details_message_id DESC
                """,

            "year_asc":
                """
                year ASC,
                details_message_id DESC
                """,

            "rating_desc":
                """
                imdb_rating IS NULL,
                imdb_rating DESC,
                details_message_id DESC
                """,

            "rating_asc":
                """
                imdb_rating IS NULL,
                imdb_rating ASC,
                details_message_id DESC
                """,
        }.get(
            sort,
            """
            details_message_id DESC
            """,
        )

        with self.connect() as db:
            count_row = db.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM movies
                {where_sql}
                """,
                params,
            ).fetchone()

            total = int(
                count_row["count"]
            )

            total_pages = max(
                1,
                (
                    total
                    + page_size
                    - 1
                )
                // page_size,
            )

            page = min(
                page,
                total_pages,
            )

            offset = (
                (page - 1)
                * page_size
            )

            movie_rows = db.execute(
                f"""
                SELECT
                    details_message_id,
                    title,
                    english_title,
                    year,
                    imdb_rating,
                    genres_json,
                    description,
                    poster_message_id

                FROM movies

                {where_sql}

                ORDER BY
                    {order_sql}

                LIMIT ?
                OFFSET ?
                """,
                [
                    *params,
                    page_size,
                    offset,
                ],
            ).fetchall()

            items: list[
                MediaItem
            ] = []

            for row in movie_rows:
                variant_rows = db.execute(
                    """
                    SELECT
                        message_id,
                        quality,
                        filename,
                        mime_type,
                        size

                    FROM variants

                    WHERE
                        details_message_id = ?

                    ORDER BY
                        CASE quality
                            WHEN '2160p'
                                THEN 5
                            WHEN '1080p'
                                THEN 4
                            WHEN '720p'
                                THEN 3
                            WHEN '576p'
                                THEN 2
                            WHEN '480p'
                                THEN 1
                            ELSE 0
                        END DESC,

                        size DESC
                    """,
                    (
                        row[
                            "details_message_id"
                        ],
                    ),
                ).fetchall()

                variants = [
                    MediaVariant(
                        message_id=(
                            variant[
                                "message_id"
                            ]
                        ),
                        quality=(
                            variant[
                                "quality"
                            ]
                        ),
                        filename=(
                            variant[
                                "filename"
                            ]
                        ),
                        mime_type=(
                            variant[
                                "mime_type"
                            ]
                        ),
                        size=(
                            variant[
                                "size"
                            ]
                        ),
                    )
                    for variant
                    in variant_rows
                ]

                try:
                    genres = normalize_genres(
                        json.loads(
                            row[
                                "genres_json"
                            ]
                        )
                    )
                except (
                    json.JSONDecodeError,
                    TypeError,
                ):
                    genres = []

                items.append(
                    MediaItem(
                        title=(
                            row["title"]
                        ),
                        year=(
                            row["year"]
                        ),
                        english_title=(
                            row[
                                "english_title"
                            ]
                        ),
                        imdb_rating=(
                            row[
                                "imdb_rating"
                            ]
                        ),
                        genres=genres,
                        description=(
                            row[
                                "description"
                            ]
                        ),
                        poster_message_id=(
                            row[
                                "poster_message_id"
                            ]
                        ),
                        details_message_id=(
                            row[
                                "details_message_id"
                            ]
                        ),
                        variants=variants,
                    )
                )

        return CatalogResult(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            has_previous=(
                page > 1
            ),
            has_next=(
                page
                < total_pages
            ),
        )
