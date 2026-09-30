import asyncio
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

load_dotenv()


@dataclass
class MediaInfo:
    type: str | None
    filename: str | None
    mime_type: str | None
    size: int | None
    duration: int | None
    width: int | None
    height: int | None


def extract_media_info(message) -> MediaInfo:
    filename = None
    duration = None
    width = None
    height = None
    mime_type = None
    size = None
    media_type = None

    document = getattr(message, "document", None)

    if document is not None:
        media_type = "document"
        mime_type = getattr(document, "mime_type", None)
        size = getattr(document, "size", None)

        for attribute in document.attributes:
            if isinstance(
                attribute,
                DocumentAttributeFilename,
            ):
                filename = attribute.file_name

            if isinstance(
                attribute,
                DocumentAttributeVideo,
            ):
                media_type = "video"
                duration = attribute.duration
                width = attribute.w
                height = attribute.h

    elif getattr(message, "photo", None):
        media_type = "photo"

    return MediaInfo(
        type=media_type,
        filename=filename,
        mime_type=mime_type,
        size=size,
        duration=duration,
        width=width,
        height=height,
    )


def extract_urls(message) -> list[str]:
    urls: list[str] = []

    for entity in message.entities or []:
        url = getattr(entity, "url", None)

        if url:
            urls.append(url)

    return urls


async def main() -> None:
    api_id = int(
        os.environ["TELEGRAM_API_ID"]
    )

    api_hash = os.environ[
        "TELEGRAM_API_HASH"
    ]

    chat_id = int(
        os.environ["TELEGRAM_SOURCE_CHAT_ID"]
    )

    session = os.getenv(
        "TELEGRAM_SESSION",
        "/shared-session/telegram-downloader",
    )

    limit = int(
        os.getenv("INSPECT_LIMIT", "30")
    )

    client = TelegramClient(
        session,
        api_id,
        api_hash,
    )

    await client.start()

    me = await client.get_me()

    print(
        f"Connected as: "
        f"{me.first_name or ''} "
        f"({me.id})"
    )

    print(
        f"Inspecting channel: {chat_id}"
    )

    print("=" * 80)

    async for message in client.iter_messages(
        chat_id,
        limit=limit,
    ):
        media = extract_media_info(
            message
        )

        record: dict[str, Any] = {
            "message_id": message.id,
            "date": (
                message.date.isoformat()
                if message.date
                else None
            ),
            "text": message.message,
            "grouped_id": message.grouped_id,
            "sender_id": message.sender_id,
            "reply_to_message_id": (
                message.reply_to_msg_id
            ),
            "media": asdict(media),
            "urls": extract_urls(message),
            "has_buttons": bool(
                message.buttons
            ),
        }

        print(
            json.dumps(
                record,
                ensure_ascii=False,
                indent=2,
            )
        )

        print("-" * 80)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
