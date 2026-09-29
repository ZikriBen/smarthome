import shutil
from pathlib import Path

import aiosqlite
from aiohttp import web

from app.config import Config
from app.database import Database


class HealthServer:
    def __init__(
        self,
        config: Config,
        database: Database,
    ):
        self.config = config
        self.database = database

    def _free_disk_gb(self) -> float:
        usage = shutil.disk_usage(
            Path(self.config.media_root)
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
        free_gb = self._free_disk_gb()

        healthy = (
            free_gb
            >= self.config.min_free_disk_gb
        )

        payload = {
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
                self.config.min_free_disk_gb
            ),
        }

        return web.json_response(
            payload,
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

            rows = await cursor.fetchall()

        counts = {
            status: count
            for status, count in rows
        }

        free_gb = self._free_disk_gb()

        return web.json_response(
            {
                "queued": counts.get(
                    "QUEUED",
                    0,
                ),
                "downloading": counts.get(
                    "DOWNLOADING",
                    0,
                ),
                "processing": counts.get(
                    "PROCESSING",
                    0,
                ),
                "retry_wait": counts.get(
                    "RETRY_WAIT",
                    0,
                ),
                "available": counts.get(
                    "AVAILABLE",
                    0,
                ),
                "failed": counts.get(
                    "FAILED",
                    0,
                ),
                "free_disk_gb": round(
                    free_gb,
                    1,
                ),
                "min_free_disk_gb": (
                    self.config.min_free_disk_gb
                ),
            }
        )

    async def start(self) -> None:
        app = web.Application()

        app.router.add_get(
            "/health",
            self.health,
        )

        app.router.add_get(
            "/status",
            self.status,
        )

        runner = web.AppRunner(app)

        await runner.setup()

        site = web.TCPSite(
            runner,
            self.config.health_host,
            self.config.health_port,
        )

        await site.start()

        print(
            "Health server listening on "
            f"{self.config.health_host}:"
            f"{self.config.health_port}",
            flush=True,
        )
