import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_chat_id: int | None
    database_path: str


def load_config() -> Config:
    chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    return Config(
        telegram_api_id=int(os.environ["TELEGRAM_API_ID"]),
        telegram_api_hash=os.environ["TELEGRAM_API_HASH"],
        telegram_chat_id=int(chat_id_raw) if chat_id_raw else None,
        database_path=os.getenv("DATABASE_PATH", "/data/downloader.db"),
    )
