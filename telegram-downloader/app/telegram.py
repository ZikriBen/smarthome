from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from app.config import Config
from app.database import Database
from app.models import DownloadJob
from app.reactions import (
    REACTION_ACCEPTED,
    set_status_reaction,
)


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

    return (
        sender_id,
        name or None,
    )


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

    async def connect(
        self,
    ) -> None:
        await self.client.start()

        me = await self.client.get_me()

        print(
            "Connected to Telegram as "
            f"{me.first_name} ({me.id})",
            flush=True,
        )

        chat = await self.client.get_entity(
            self.config.telegram_chat_id
        )

        print(
            "Listening to Telegram chat "
            f"{getattr(chat, 'title', '')} "
            f"({self.config.telegram_chat_id})",
            flush=True,
        )

        # Important:
        #
        # Do NOT use events.NewMessage(chats=...).
        #
        # We already verified that Telethon's chat filter
        # does not reliably fire for this ZikriMedia chat.
        #
        # Listen globally and filter ourselves instead.
        self.client.add_event_handler(
            self.on_new_message,
            events.NewMessage(),
        )

    async def run(
        self,
    ) -> None:
        await (
            self.client
            .run_until_disconnected()
        )

    async def enqueue_message(
        self,
        message: Message,
        *,
        chat_id: int,
        react_on_accept: bool,
    ) -> tuple[
        bool,
        DownloadJob | None,
        str,
    ]:
        if not contains_downloadable_media(
            message
        ):
            return (
                False,
                None,
                "message_has_no_downloadable_media",
            )

        chat = await message.get_chat()

        sender_id, sender_name = (
            await get_sender_name(
                message
            )
        )

        job = DownloadJob(
            telegram_chat_id=chat_id,

            telegram_chat_name=getattr(
                chat,
                "title",
                None,
            ),

            telegram_message_id=(
                message.id
            ),

            telegram_media_group_id=(
                message.grouped_id
            ),

            sender_id=sender_id,

            sender_name=sender_name,

            caption=(
                message.text
                or None
            ),

            original_filename=(
                get_filename(
                    message
                )
            ),

            file_size=(
                get_file_size(
                    message
                )
            ),
        )

        created = (
            await self.database
            .create_job(
                job
            )
        )

        if not created:
            print(
                "Ignoring duplicate message "
                f"{chat_id}/"
                f"{message.id}",
                flush=True,
            )

            return (
                False,
                job,
                "duplicate",
            )

        print(
            "Accepted job "
            f"{chat_id}/"
            f"{message.id} "
            f"{job.original_filename}",
            flush=True,
        )

        if react_on_accept:
            await set_status_reaction(
                self.client,
                chat_id,
                message.id,
                REACTION_ACCEPTED,
            )

        return (
            True,
            job,
            "created",
        )

    async def enqueue_message_id(
        self,
        chat_id: int,
        message_id: int,
    ) -> tuple[
        bool,
        DownloadJob | None,
        str,
    ]:
        """
        Browser/API entry point.

        Browser source channels are read-only:
        no reaction is attempted.
        """

        try:
            message = (
                await self.client
                .get_messages(
                    chat_id,
                    ids=message_id,
                )
            )

        except Exception as exc:
            print(
                "Failed to fetch Telegram "
                f"message {chat_id}/"
                f"{message_id}: "
                f"{type(exc).__name__}: "
                f"{exc}",
                flush=True,
            )

            return (
                False,
                None,
                "message_fetch_failed",
            )

        if message is None:
            return (
                False,
                None,
                "message_not_found",
            )

        return (
            await self.enqueue_message(
                message,
                chat_id=chat_id,
                react_on_accept=False,
            )
        )

    async def on_new_message(
        self,
        event: events.NewMessage.Event,
    ) -> None:
        chat_id = event.chat_id

        # Silently ignore all unrelated Telegram chats.
        if (
            chat_id
            != self.config.telegram_chat_id
        ):
            return

        message = event.message

        if not contains_downloadable_media(
            message
        ):
            return

        await self.enqueue_message(
            message,
            chat_id=chat_id,
            react_on_accept=True,
        )
