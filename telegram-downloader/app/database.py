from pathlib import Path

import aiosqlite

from app.models import (
    DownloadJob,
    JobRecord,
    JobStatus,
)


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
        Path(self.path).parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        async with aiosqlite.connect(self.path) as db:
            await db.execute(SCHEMA)
            await db.commit()

    async def recover_incomplete_jobs(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            result = await db.execute(
                """
                UPDATE download_jobs
                SET status = ?
                WHERE status IN (?, ?, ?)
                """,
                (
                    JobStatus.QUEUED.value,
                    JobStatus.DOWNLOADING.value,
                    JobStatus.PROCESSING.value,
                    JobStatus.RETRY_WAIT.value,
                ),
            )

            await db.commit()

            if result.rowcount:
                print(
                    f"Recovered {result.rowcount} "
                    "incomplete job(s)"
                )

    async def create_job(
        self,
        job: DownloadJob,
    ) -> bool:
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

    async def claim_next_job(
        self,
    ) -> JobRecord | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row

            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT *
                FROM download_jobs
                WHERE status = ?
                ORDER BY id
                LIMIT 1
                """,
                (JobStatus.QUEUED.value,),
            )

            row = await cursor.fetchone()

            if row is None:
                await db.commit()
                return None

            await db.execute(
                """
                UPDATE download_jobs
                SET
                    status = ?,
                    started_at = COALESCE(
                        started_at,
                        CURRENT_TIMESTAMP
                    )
                WHERE id = ?
                """,
                (
                    JobStatus.DOWNLOADING.value,
                    row["id"],
                ),
            )

            await db.commit()

            return self._row_to_job(row)

    async def increment_attempt(
        self,
        job_id: int,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE download_jobs
                SET attempt_count = attempt_count + 1
                WHERE id = ?
                """,
                (job_id,),
            )

            await db.commit()

    async def mark_retry(
        self,
        job_id: int,
        error: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE download_jobs
                SET
                    status = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (
                    JobStatus.RETRY_WAIT.value,
                    error,
                    job_id,
                ),
            )

            await db.commit()

    async def mark_downloading(
        self,
        job_id: int,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE download_jobs
                SET status = ?
                WHERE id = ?
                """,
                (
                    JobStatus.DOWNLOADING.value,
                    job_id,
                ),
            )

            await db.commit()

    async def mark_processing(
        self,
        job_id: int,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE download_jobs
                SET status = ?
                WHERE id = ?
                """,
                (
                    JobStatus.PROCESSING.value,
                    job_id,
                ),
            )

            await db.commit()

    async def mark_available(
        self,
        job_id: int,
        local_path: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE download_jobs
                SET
                    status = ?,
                    local_path = ?,
                    completed_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                WHERE id = ?
                """,
                (
                    JobStatus.AVAILABLE.value,
                    local_path,
                    job_id,
                ),
            )

            await db.commit()

    async def mark_failed(
        self,
        job_id: int,
        error: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE download_jobs
                SET
                    status = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (
                    JobStatus.FAILED.value,
                    error,
                    job_id,
                ),
            )

            await db.commit()

    @staticmethod
    def _row_to_job(
        row: aiosqlite.Row,
    ) -> JobRecord:
        return JobRecord(
            id=row["id"],
            telegram_chat_id=row["telegram_chat_id"],
            telegram_chat_name=row["telegram_chat_name"],
            telegram_message_id=row[
                "telegram_message_id"
            ],
            telegram_media_group_id=row[
                "telegram_media_group_id"
            ],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            caption=row["caption"],
            original_filename=row[
                "original_filename"
            ],
            file_size=row["file_size"],
            status=JobStatus(row["status"]),
            local_path=row["local_path"],
            attempt_count=row["attempt_count"],
        )
