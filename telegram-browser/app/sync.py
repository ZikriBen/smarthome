import asyncio

from telethon import TelegramClient

from app.catalog import (
    build_catalog_from_messages,
)
from app.database import (
    CatalogDatabase,
)


class CatalogSyncer:
    OVERLAP_MESSAGES = 20

    def __init__(
        self,
        *,
        client: TelegramClient,
        database: CatalogDatabase,
        chat_id: int,
        initial_messages: int,
        backfill_messages: int,
    ):
        self.client = client
        self.database = database
        self.chat_id = chat_id

        self.initial_messages = (
            initial_messages
        )

        self.backfill_messages = (
            backfill_messages
        )

        self.lock = asyncio.Lock()

    async def initialize(
        self,
    ) -> int:
        if (
            self.database
            .count_movies()
            > 0
        ):
            return 0

        async with self.lock:
            messages = [
                message
                async for message
                in self.client.iter_messages(
                    self.chat_id,
                    limit=(
                        self.initial_messages
                    ),
                )
            ]

            if not messages:
                self.database.set_state(
                    "history_complete",
                    True,
                )

                return 0

            items = (
                build_catalog_from_messages(
                    messages
                )
            )

            self.database.upsert_items(
                items
            )

            newest = max(
                message.id
                for message in messages
            )

            oldest = min(
                message.id
                for message in messages
            )

            self.database.set_state(
                "newest_scanned_message_id",
                newest,
            )

            self.database.set_state(
                "oldest_scanned_message_id",
                oldest,
            )

            history_complete = (
                len(messages)
                < self.initial_messages
            )

            self.database.set_state(
                "history_complete",
                history_complete,
            )

            print(
                "Initial catalog index: "
                f"{len(items)} movie(s), "
                f"{len(messages)} Telegram "
                "message(s), "
                f"history_complete="
                f"{history_complete}",
                flush=True,
            )

            return len(
                items
            )

    async def sync_newer(
        self,
    ) -> int:
        async with self.lock:
            newest = (
                self.database
                .get_state_int(
                    "newest_scanned_message_id"
                )
            )

            if newest is None:
                return 0

            min_id = max(
                0,
                newest
                - self.OVERLAP_MESSAGES,
            )

            messages = [
                message
                async for message
                in self.client.iter_messages(
                    self.chat_id,
                    min_id=min_id,
                )
            ]

            if not messages:
                return 0

            items = (
                build_catalog_from_messages(
                    messages
                )
            )

            self.database.upsert_items(
                items
            )

            newest_seen = max(
                message.id
                for message in messages
            )

            if newest_seen > newest:
                self.database.set_state(
                    "newest_scanned_message_id",
                    newest_seen,
                )

            new_message_count = sum(
                1
                for message in messages
                if message.id > newest
            )

            if new_message_count:
                print(
                    "Newer sync: "
                    f"{new_message_count} new "
                    "Telegram message(s), "
                    f"{len(items)} movie(s) "
                    "parsed",
                    flush=True,
                )

            return new_message_count

    async def backfill(
        self,
    ) -> int:
        if (
            self.database
            .get_state_bool(
                "history_complete"
            )
        ):
            return 0

        async with self.lock:
            oldest = (
                self.database
                .get_state_int(
                    "oldest_scanned_message_id"
                )
            )

            if oldest is None:
                return 0

            max_id = (
                oldest
                + self.OVERLAP_MESSAGES
            )

            limit = (
                self.backfill_messages
                + self.OVERLAP_MESSAGES
            )

            messages = [
                message
                async for message
                in self.client.iter_messages(
                    self.chat_id,
                    max_id=max_id,
                    limit=limit,
                )
            ]

            if not messages:
                self.database.set_state(
                    "history_complete",
                    True,
                )

                print(
                    "Catalog history indexing "
                    "complete",
                    flush=True,
                )

                return 0

            items = (
                build_catalog_from_messages(
                    messages
                )
            )

            self.database.upsert_items(
                items
            )

            oldest_seen = min(
                message.id
                for message in messages
            )

            moved_back = (
                oldest_seen
                < oldest
            )

            if moved_back:
                self.database.set_state(
                    "oldest_scanned_message_id",
                    oldest_seen,
                )

            history_complete = (
                len(messages)
                < limit
                or not moved_back
            )

            if history_complete:
                self.database.set_state(
                    "history_complete",
                    True,
                )

            print(
                "History backfill: "
                f"{len(items)} movie(s), "
                f"{len(messages)} Telegram "
                f"message(s), "
                f"oldest={oldest_seen}, "
                f"movies_total="
                f"{self.database.count_movies()}, "
                f"complete="
                f"{history_complete}",
                flush=True,
            )

            return len(
                items
            )

    async def run_background(
        self,
        *,
        newer_interval_seconds: int,
        backfill_pause_seconds: float,
    ) -> None:
        """
        Continuously:

        1. Backfill old Telegram history until complete.
        2. Periodically check for newly-posted movies.

        Only one Telegram sync operation runs at once.
        """

        last_newer_check = 0.0

        loop = (
            asyncio.get_running_loop()
        )

        while True:
            try:
                now = loop.time()

                if (
                    now
                    - last_newer_check
                    >= newer_interval_seconds
                ):
                    await self.sync_newer()

                    last_newer_check = (
                        loop.time()
                    )

                if not (
                    self.database
                    .get_state_bool(
                        "history_complete"
                    )
                ):
                    await self.backfill()

                    await asyncio.sleep(
                        backfill_pause_seconds
                    )

                    continue

                await asyncio.sleep(
                    min(
                        newer_interval_seconds,
                        30,
                    )
                )

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                print(
                    "Background catalog sync "
                    "error: "
                    f"{type(exc).__name__}: "
                    f"{exc}",
                    flush=True,
                )

                await asyncio.sleep(
                    10
                )
