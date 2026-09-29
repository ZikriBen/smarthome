import asyncio

from app.config import Config
from app.database import Database
from app.downloader import Downloader


class DownloadQueue:
    def __init__(
        self,
        config: Config,
        database: Database,
        downloader: Downloader,
    ):
        self.config = config
        self.database = database
        self.downloader = downloader

    async def worker(
        self,
        worker_id: int,
    ) -> None:
        print(
            f"Download worker {worker_id} started",
            flush=True,
        )

        while True:
            job = (
                await self.database.claim_next_job()
            )

            if job is None:
                await asyncio.sleep(2)
                continue

            print(
                f"Worker {worker_id} "
                f"claimed job {job.id}",
                flush=True,
            )

            try:
                await self.downloader.process(job)

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                print(
                    f"Worker {worker_id} "
                    f"unexpected error: {exc}",
                    flush=True,
                )

    async def run(self) -> None:
        workers = [
            asyncio.create_task(
                self.worker(worker_id)
            )
            for worker_id in range(
                1,
                self.config.max_concurrent_downloads
                + 1,
            )
        ]

        await asyncio.gather(*workers)
