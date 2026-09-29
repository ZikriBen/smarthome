from dataclasses import dataclass
from enum import StrEnum


class JobStatus(StrEnum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    PROCESSING = "PROCESSING"
    AVAILABLE = "AVAILABLE"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"


@dataclass
class DownloadJob:
    telegram_chat_id: int
    telegram_chat_name: str | None
    telegram_message_id: int
    telegram_media_group_id: int | None

    sender_id: int | None
    sender_name: str | None

    caption: str | None
    original_filename: str | None
    file_size: int | None

    status: JobStatus = JobStatus.QUEUED
