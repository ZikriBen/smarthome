from pathlib import Path

import aiosqlite

from app.models import DownloadJob


SCHEMA = """
CREATE TABLE IF NOT EXISTS download_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    telegram_chat_id INTEGER NOT NULL,
    telegram_chat_name TEXT,
    telegram_message_id INTEGER NOT NULL,
    telegram_media_group_id INTEGER,

    sender_id INTEGER,
    sender_name TEXT,

    caption TEXT,
    original_filename TEXT,
    file_size INTEGER,

    status TEXT NOT NULL,

    local_path TEXT,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME,
    completed_at DATETIME,

    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,

    UNIQUE (telegram_chat_id, telegram_message_id)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.path) as db:
            await db.execute(SCHEMA)
            await db.commit()

    async def create_job(self, job: DownloadJob) -> bool:
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    """
                    INSERT INTO download_jobs (
                        telegram_chat_id,
                        telegram_chat_name,
                        telegram_message_id,
                        telegram_media_group_id,
                        sender_id,
                        sender_name,
                        caption,
                        original_filename,
                        file_size,
                        status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.telegram_chat_id,
                        job.telegram_chat_name,
                        job.telegram_message_id,
                        job.telegram_media_group_id,
                        job.sender_id,
                        job.sender_name,
                        job.caption,
                        job.original_filename,
                        job.file_size,
                        job.status.value,
                    ),
                )

                await db.commit()

            return True

        except aiosqlite.IntegrityError:
            return False
