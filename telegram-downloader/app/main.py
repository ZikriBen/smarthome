import asyncio

from app.config import load_config
from app.database import Database
from app.downloader import Downloader
from app.health import HealthServer
from app.queue import DownloadQueue
from app.telegram import TelegramService


async def main() -> None:
    config = load_config()

    database = Database(
        config.database_path
    )

    await database.initialize()

    await (
        database
        .recover_incomplete_jobs()
    )

    telegram = TelegramService(
        config=config,
        database=database,
    )

    await telegram.connect()

    downloader = Downloader(
        config=config,
        database=database,
        client=telegram.client,
    )

    queue = DownloadQueue(
        config=config,
        database=database,
        downloader=downloader,
    )

    health = HealthServer(
        config=config,
        database=database,
        telegram=telegram,
    )

    await health.start()

    print(
        "Startup complete: "
        f"{config.max_concurrent_downloads} "
        "workers, "
        "disk minimum "
        f"{config.min_free_disk_gb} GB",
        flush=True,
    )

    await asyncio.gather(
        telegram.run(),
        queue.run(),
    )


if __name__ == "__main__":
    asyncio.run(
        main()
    )
