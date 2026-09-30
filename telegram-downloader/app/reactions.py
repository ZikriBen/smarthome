from telethon import TelegramClient
from telethon.tl.functions.messages import (
    SendReactionRequest,
)
from telethon.tl.types import ReactionEmoji


REACTION_ACCEPTED = "👀"
REACTION_AVAILABLE = "👍"
REACTION_FAILED = "👎"


async def set_status_reaction(
    client: TelegramClient,
    chat_id: int,
    message_id: int,
    emoji: str,
) -> None:
    try:
        entity = await client.get_input_entity(
            chat_id
        )

        await client(
            SendReactionRequest(
                peer=entity,
                msg_id=message_id,
                reaction=[],
                big=False,
                add_to_recent=False,
            )
        )

        await client(
            SendReactionRequest(
                peer=entity,
                msg_id=message_id,
                reaction=[
                    ReactionEmoji(
                        emoticon=emoji
                    )
                ],
                big=False,
                add_to_recent=False,
            )
        )

        print(
            f"Reaction updated "
            f"{chat_id}/{message_id} "
            f"-> {emoji}",
            flush=True,
        )

    except Exception as exc:
        print(
            f"Telegram reaction failed "
            f"{chat_id}/{message_id}: "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )
