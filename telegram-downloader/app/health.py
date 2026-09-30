import math
import shutil
from pathlib import Path

import aiosqlite
from aiohttp import web

from app.config import Config
from app.database import Database
from app.telegram import TelegramService


VALID_JOB_STATUSES = {
    "QUEUED",
    "DOWNLOADING",
    "PROCESSING",
    "RETRY_WAIT",
    "AVAILABLE",
    "FAILED",
}


class HealthServer:
    def __init__(
        self,
        config: Config,
        database: Database,
        telegram: TelegramService,
    ):
        self.config = config
        self.database = database
        self.telegram = telegram

    def _free_disk_gb(
        self,
    ) -> float:
        usage = shutil.disk_usage(
            Path(
                self.config.media_root
            )
        )

        return (
            usage.free
            / 1024
            / 1024
            / 1024
        )

    async def health(
        self,
        request: web.Request,
    ) -> web.Response:
        free_gb = (
            self._free_disk_gb()
        )

        healthy = (
            free_gb
            >= self.config.min_free_disk_gb
        )

        return web.json_response(
            {
                "status": (
                    "ok"
                    if healthy
                    else "degraded"
                ),
                "free_disk_gb": round(
                    free_gb,
                    1,
                ),
                "min_free_disk_gb": (
                    self.config
                    .min_free_disk_gb
                ),
            },
            status=(
                200
                if healthy
                else 503
            ),
        )

    async def status(
        self,
        request: web.Request,
    ) -> web.Response:
        async with aiosqlite.connect(
            self.database.path
        ) as db:
            cursor = await db.execute(
                """
                SELECT
                    status,
                    COUNT(*)
                FROM download_jobs
                GROUP BY status
                """
            )

            rows = (
                await cursor.fetchall()
            )

        counts = {
            status: count
            for status, count
            in rows
        }

        free_gb = (
            self._free_disk_gb()
        )

        return web.json_response(
            {
                "queued":
                    counts.get(
                        "QUEUED",
                        0,
                    ),

                "downloading":
                    counts.get(
                        "DOWNLOADING",
                        0,
                    ),

                "processing":
                    counts.get(
                        "PROCESSING",
                        0,
                    ),

                "retry_wait":
                    counts.get(
                        "RETRY_WAIT",
                        0,
                    ),

                "available":
                    counts.get(
                        "AVAILABLE",
                        0,
                    ),

                "failed":
                    counts.get(
                        "FAILED",
                        0,
                    ),

                "free_disk_gb":
                    round(
                        free_gb,
                        1,
                    ),

                "min_free_disk_gb": (
                    self.config
                    .min_free_disk_gb
                ),
            }
        )

    async def enqueue(
        self,
        request: web.Request,
    ) -> web.Response:
        try:
            payload = (
                await request.json()
            )

        except Exception:
            return web.json_response(
                {
                    "status": "error",
                    "error": "invalid_json",
                },
                status=400,
            )

        chat_id = payload.get(
            "chat_id"
        )

        message_id = payload.get(
            "message_id"
        )

        if (
            isinstance(
                chat_id,
                bool,
            )
            or not isinstance(
                chat_id,
                int,
            )
        ):
            return web.json_response(
                {
                    "status": "error",
                    "error": "invalid_chat_id",
                },
                status=400,
            )

        if (
            isinstance(
                message_id,
                bool,
            )
            or not isinstance(
                message_id,
                int,
            )
            or message_id <= 0
        ):
            return web.json_response(
                {
                    "status": "error",
                    "error": "invalid_message_id",
                },
                status=400,
            )

        try:
            (
                created,
                job,
                reason,
            ) = (
                await self.telegram
                .enqueue_message_id(
                    chat_id,
                    message_id,
                )
            )

        except Exception as exc:
            print(
                "HTTP enqueue failed for "
                f"{chat_id}/"
                f"{message_id}: "
                f"{type(exc).__name__}: "
                f"{exc}",
                flush=True,
            )

            return web.json_response(
                {
                    "status": "error",
                    "error": "enqueue_failed",
                },
                status=500,
            )

        if reason == "message_not_found":
            return web.json_response(
                {
                    "status": "error",
                    "error": reason,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
                status=404,
            )

        if reason == "message_fetch_failed":
            return web.json_response(
                {
                    "status": "error",
                    "error": reason,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
                status=502,
            )

        if reason in (
            "message_has_no_downloadable_media",
            "chat_id_unavailable",
        ):
            return web.json_response(
                {
                    "status": "error",
                    "error": reason,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
                status=400,
            )

        if reason == "duplicate":
            return web.json_response(
                {
                    "status":
                        "already_exists",

                    "chat_id":
                        chat_id,

                    "message_id":
                        message_id,

                    "filename": (
                        job.original_filename
                        if job
                        else None
                    ),
                }
            )

        return web.json_response(
            {
                "status":
                    "queued",

                "chat_id":
                    chat_id,

                "message_id":
                    message_id,

                "filename": (
                    job.original_filename
                    if job
                    else None
                ),
            },
            status=201,
        )

    async def job_statuses(
        self,
        request: web.Request,
    ) -> web.Response:
        try:
            payload = (
                await request.json()
            )

        except Exception:
            return web.json_response(
                {
                    "status": "error",
                    "error": "invalid_json",
                },
                status=400,
            )

        chat_id = payload.get(
            "chat_id"
        )

        message_ids = payload.get(
            "message_ids"
        )

        if (
            isinstance(
                chat_id,
                bool,
            )
            or not isinstance(
                chat_id,
                int,
            )
        ):
            return web.json_response(
                {
                    "status": "error",
                    "error": "invalid_chat_id",
                },
                status=400,
            )

        if not isinstance(
            message_ids,
            list,
        ):
            return web.json_response(
                {
                    "status": "error",
                    "error": "invalid_message_ids",
                },
                status=400,
            )

        clean_ids = []

        for value in message_ids:
            if (
                isinstance(
                    value,
                    bool,
                )
                or not isinstance(
                    value,
                    int,
                )
                or value <= 0
            ):
                continue

            clean_ids.append(
                value
            )

        clean_ids = list(
            dict.fromkeys(
                clean_ids
            )
        )

        if len(clean_ids) > 200:
            return web.json_response(
                {
                    "status": "error",
                    "error": "too_many_message_ids",
                },
                status=400,
            )

        if not clean_ids:
            return web.json_response(
                {
                    "jobs": {}
                }
            )

        placeholders = ",".join(
            "?"
            for _ in clean_ids
        )

        query = f"""
            SELECT
                id,
                telegram_message_id,
                status,
                local_path,
                attempt_count,
                last_error,
                created_at,
                started_at,
                completed_at
            FROM download_jobs
            WHERE
                telegram_chat_id = ?
                AND telegram_message_id
                    IN ({placeholders})
        """

        params = [
            chat_id,
            *clean_ids,
        ]

        async with aiosqlite.connect(
            self.database.path
        ) as db:
            db.row_factory = (
                aiosqlite.Row
            )

            cursor = await db.execute(
                query,
                params,
            )

            rows = (
                await cursor.fetchall()
            )

        jobs = {}

        for row in rows:
            message_id = row[
                "telegram_message_id"
            ]

            jobs[
                str(message_id)
            ] = {
                "job_id":
                    row["id"],

                "status":
                    row["status"],

                "local_path":
                    row["local_path"],

                "attempt_count":
                    row["attempt_count"],

                "last_error":
                    row["last_error"],

                "created_at":
                    row["created_at"],

                "started_at":
                    row["started_at"],

                "completed_at":
                    row["completed_at"],
            }

        return web.json_response(
            {
                "jobs": jobs
            }
        )

    async def jobs(
        self,
        request: web.Request,
    ) -> web.Response:
        """
        Paginated job history.

        GET /jobs
            ?chat_id=-1001075658842
            &page=1
            &page_size=30
            &status=DOWNLOADING
        """

        try:
            chat_id = int(
                request.query[
                    "chat_id"
                ]
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            return web.json_response(
                {
                    "status": "error",
                    "error": "invalid_chat_id",
                },
                status=400,
            )

        try:
            page = max(
                1,
                int(
                    request.query.get(
                        "page",
                        "1",
                    )
                ),
            )

            page_size = int(
                request.query.get(
                    "page_size",
                    "30",
                )
            )

        except ValueError:
            return web.json_response(
                {
                    "status": "error",
                    "error": "invalid_pagination",
                },
                status=400,
            )

        page_size = max(
            1,
            min(
                page_size,
                100,
            ),
        )

        status_filter = (
            request.query.get(
                "status",
                "",
            )
            .strip()
            .upper()
        )

        if (
            status_filter
            and status_filter
            not in VALID_JOB_STATUSES
        ):
            return web.json_response(
                {
                    "status": "error",
                    "error": "invalid_status",
                },
                status=400,
            )

        where = [
            "telegram_chat_id = ?"
        ]

        params = [
            chat_id
        ]

        if status_filter:
            where.append(
                "status = ?"
            )

            params.append(
                status_filter
            )

        where_sql = " AND ".join(
            where
        )

        offset = (
            page - 1
        ) * page_size

        async with aiosqlite.connect(
            self.database.path
        ) as db:
            db.row_factory = (
                aiosqlite.Row
            )

            count_cursor = await db.execute(
                f"""
                SELECT COUNT(*)
                FROM download_jobs
                WHERE {where_sql}
                """,
                params,
            )

            total = (
                await count_cursor.fetchone()
            )[0]

            jobs_cursor = await db.execute(
                f"""
                SELECT
                    id,
                    telegram_chat_id,
                    telegram_message_id,
                    caption,
                    original_filename,
                    file_size,
                    status,
                    local_path,
                    attempt_count,
                    last_error,
                    created_at,
                    started_at,
                    completed_at
                FROM download_jobs
                WHERE {where_sql}
                ORDER BY id DESC
                LIMIT ?
                OFFSET ?
                """,
                [
                    *params,
                    page_size,
                    offset,
                ],
            )

            rows = (
                await jobs_cursor.fetchall()
            )

            counts_cursor = await db.execute(
                """
                SELECT
                    status,
                    COUNT(*) AS count
                FROM download_jobs
                WHERE telegram_chat_id = ?
                GROUP BY status
                """,
                (
                    chat_id,
                ),
            )

            count_rows = (
                await counts_cursor.fetchall()
            )

        total_pages = max(
            1,
            math.ceil(
                total
                / page_size
            ),
        )

        counts = {
            row["status"]:
                row["count"]
            for row in count_rows
        }

        return web.json_response(
            {
                "items": [
                    {
                        "id":
                            row["id"],

                        "chat_id":
                            row[
                                "telegram_chat_id"
                            ],

                        "message_id":
                            row[
                                "telegram_message_id"
                            ],

                        "caption":
                            row["caption"],

                        "filename":
                            row[
                                "original_filename"
                            ],

                        "file_size":
                            row["file_size"],

                        "status":
                            row["status"],

                        "local_path":
                            row["local_path"],

                        "attempt_count":
                            row[
                                "attempt_count"
                            ],

                        "last_error":
                            row["last_error"],

                        "created_at":
                            row["created_at"],

                        "started_at":
                            row["started_at"],

                        "completed_at":
                            row["completed_at"],
                    }
                    for row in rows
                ],

                "pagination": {
                    "page":
                        page,

                    "page_size":
                        page_size,

                    "total":
                        total,

                    "total_pages":
                        total_pages,

                    "has_previous":
                        page > 1,

                    "has_next":
                        page < total_pages,
                },

                "counts": {
                    "QUEUED":
                        counts.get(
                            "QUEUED",
                            0,
                        ),

                    "DOWNLOADING":
                        counts.get(
                            "DOWNLOADING",
                            0,
                        ),

                    "PROCESSING":
                        counts.get(
                            "PROCESSING",
                            0,
                        ),

                    "RETRY_WAIT":
                        counts.get(
                            "RETRY_WAIT",
                            0,
                        ),

                    "AVAILABLE":
                        counts.get(
                            "AVAILABLE",
                            0,
                        ),

                    "FAILED":
                        counts.get(
                            "FAILED",
                            0,
                        ),
                },
            }
        )

    async def start(
        self,
    ) -> None:
        app = web.Application()

        app.router.add_get(
            "/health",
            self.health,
        )

        app.router.add_get(
            "/status",
            self.status,
        )

        app.router.add_post(
            "/enqueue",
            self.enqueue,
        )

        app.router.add_post(
            "/jobs/status",
            self.job_statuses,
        )

        app.router.add_get(
            "/jobs",
            self.jobs,
        )

        runner = web.AppRunner(
            app
        )

        await runner.setup()

        site = web.TCPSite(
            runner,
            self.config.health_host,
            self.config.health_port,
        )

        await site.start()

        print(
            "Health/API server listening on "
            f"{self.config.health_host}:"
            f"{self.config.health_port}",
            flush=True,
        )
