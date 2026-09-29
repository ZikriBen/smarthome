from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from app.config import Config
from app.database import Database
from app.models import DownloadJob
from app.notifications import accepted_message


def get_filename(
    message: Message,
) -> str | None:
    if message.file:
        return message.file.name

    return None


def get_file_size(
    message: Message,
) -> int | None:
    if message.file:
        return message.file.size

    return None


async def get_sender_name(
    message: Message,
) -> tuple[int | None, str | None]:
    sender = await message.get_sender()

    if not sender:
        return None, None

    sender_id = getattr(
        sender,
        "id",
        None,
    )

    first_name = getattr(
        sender,
        "first_name",
        None,
    )

    last_name = getattr(
        sender,
        "last_name",
        None,
    )

    username = getattr(
        sender,
        "username",
        None,
    )

    name = " ".join(
        part
        for part in (
            first_name,
            last_name,
        )
        if part
    )

    if not name and username:
        name = f"@{username}"

    return sender_id, name or None


def contains_downloadable_media(
    message: Message,
) -> bool:
    if not message.media:
        return False

    if message.video:
        return True

    if message.document:
        return True

    return False


class TelegramService:
    def __init__(
        self,
        config: Config,
        database: Database,
    ):
        self.config = config
        self.database = database

        self.client = TelegramClient(
            "/data/telegram-downloader",
            config.telegram_api_id,
            config.telegram_api_hash,
        )

    async def connect(self) -> None:
        await self.client.start()

        me = await self.client.get_me()

        print(
            "Connected to Telegram as "
            f"{me.first_name} ({me.id})",
            flush=True,
        )

        self.client.add_event_handler(
            self.on_new_message,
            events.NewMessage(
                chats=self.config.telegram_chat_id,
            ),
        )

        print(
            "Listening to Telegram chat "
            f"{self.config.telegram_chat_id}",
            flush=True,
        )

    async def run(self) -> None:
        await self.client.run_until_disconnected()

    async def on_new_message(
        self,
        event: events.NewMessage.Event,
    ) -> None:
        message = event.message

        if not contains_downloadable_media(
            message
        ):
            return

        chat = await message.get_chat()

        sender_id, sender_name = (
            await get_sender_name(message)
        )

        job = DownloadJob(
            telegram_chat_id=event.chat_id,
            telegram_chat_name=getattr(
                chat,
                "title",
                None,
            ),
            telegram_message_id=message.id,
            telegram_media_group_id=(
                message.grouped_id
            ),
            sender_id=sender_id,
            sender_name=sender_name,
            caption=message.text or None,
            original_filename=(
                get_filename(message)
            ),
            file_size=get_file_size(message),
        )

        created = (
            await self.database.create_job(job)
        )

        if not created:
            print(
                "Ignoring duplicate message "
                f"{event.chat_id}/"
                f"{message.id}",
                flush=True,
            )
            return

        print(
            "Accepted job "
            f"{event.chat_id}/"
            f"{message.id} "
            f"{job.original_filename}",
            flush=True,
        )

        await message.reply(
            accepted_message(
                job.original_filename,
                job.file_size,
            )
        )
