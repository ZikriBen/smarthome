import asyncio

from app.config import load_config
from app.database import Database
from app.telegram import TelegramService


async def main() -> None:
    config = load_config()

    database = Database(config.database_path)
    await database.initialize()

    telegram = TelegramService(
        config=config,
        database=database,
    )

    await telegram.start()


if __name__ == "__main__":
    asyncio.run(main())
