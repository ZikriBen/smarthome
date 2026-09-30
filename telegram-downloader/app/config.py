import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_chat_id: int

    database_path: str
    media_root: str

    max_concurrent_downloads: int
    max_retries: int
    retry_base_seconds: int

    telegram_download_connections: int

    min_free_disk_gb: int

    health_host: str
    health_port: int

    jellyfin_url: str
    jellyfin_api_key: str


def load_config() -> Config:
    return Config(
        telegram_api_id=int(
            os.environ["TELEGRAM_API_ID"]
        ),
        telegram_api_hash=os.environ[
            "TELEGRAM_API_HASH"
        ],
        telegram_chat_id=int(
            os.environ["TELEGRAM_CHAT_ID"]
        ),

        database_path=os.getenv(
            "DATABASE_PATH",
            "/data/downloader.db",
        ),

        media_root=os.getenv(
            "MEDIA_ROOT",
            "/media",
        ),

        max_concurrent_downloads=int(
            os.getenv(
                "MAX_CONCURRENT_DOWNLOADS",
                "5",
            )
        ),

        max_retries=int(
            os.getenv(
                "MAX_RETRIES",
                "5",
            )
        ),

        retry_base_seconds=int(
            os.getenv(
                "RETRY_BASE_SECONDS",
                "5",
            )
        ),

        telegram_download_connections=int(
            os.getenv(
                "TELEGRAM_DOWNLOAD_CONNECTIONS",
                "2",
            )
        ),

        min_free_disk_gb=int(
            os.getenv(
                "MIN_FREE_DISK_GB",
                "50",
            )
        ),

        health_host=os.getenv(
            "HEALTH_HOST",
            "0.0.0.0",
        ),

        health_port=int(
            os.getenv(
                "HEALTH_PORT",
                "8787",
            )
        ),

        jellyfin_url=os.getenv(
            "JELLYFIN_URL",
            "http://10.0.0.13:8096",
        ).rstrip("/"),

        jellyfin_api_key=os.getenv(
            "JELLYFIN_API_KEY",
            "",
        ),
    )
