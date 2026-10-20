"""otp-relay: take one-time SMS codes posted by Ben's forward bot out of the Telegram stream BEFORE auth and
before the agent, and drop them into the shared OTP inbox for the Command Center.

Every message from the forward bot (OTP_FORWARD_BOT_ID) is consumed here and never reaches auth, the session
transcript, or the LLM. Only a message in the allowlisted chat (OTP_FORWARD_CHAT_ID; for a bot-to-bot DM that
is the forward bot's own id, the default) whose whole text is exactly `#OTP <site> <code>` is stored. Messages
from anyone else are untouched. The code is never logged. With OTP_FORWARD_BOT_ID unset the plugin is inert.
"""

import logging
import os
import re

try:
    from . import otp_inbox
except ImportError:  # loaded as a plain module (tests)
    import otp_inbox  # type: ignore

logger = logging.getLogger(__name__)
MESSAGE_RE = re.compile(r"#OTP ([a-z_]{1,32}) (\d{4,8})")
SKIP = {"action": "skip", "reason": "otp-relay"}


def _config():
    bot = os.environ.get("OTP_FORWARD_BOT_ID", "").strip()
    chat = os.environ.get("OTP_FORWARD_CHAT_ID", "").strip() or bot
    path = os.environ.get("OTP_INBOX_DB", "/opt/data/otp/codes.sqlite")
    return bot, chat, path


def _platform_name(source):
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").lower()


def on_pre_gateway_dispatch(event=None, **_kwargs):
    bot, chat, path = _config()
    source = getattr(event, "source", None)
    if not bot.isdigit() or source is None or _platform_name(source) != "telegram":
        return None
    if str(getattr(source, "user_id", "") or "") != bot:
        return None
    text = str(getattr(event, "text", "") or "").strip()
    match = MESSAGE_RE.fullmatch(text)
    if match is None or str(getattr(source, "chat_id", "") or "") != chat:
        logger.info("otp-relay: dropped a message from the forward bot (not in the strict format/chat)")
        return SKIP
    site, code = match.groups()
    if not otp_inbox.valid(site, code):
        logger.info("otp-relay: dropped a code for an unknown site or of the wrong length")
        return SKIP
    message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
    try:
        stored = otp_inbox.put(path, site, code, ref=f"tg:{chat}:{message_id}" if message_id else None)
    except Exception as exc:  # never let a store problem leak the message to the agent
        logger.warning("otp-relay: inbox write failed (%s)", type(exc).__name__)
        return SKIP
    logger.info("otp-relay: %s code for %s", "stored" if stored else "ignored duplicate", site)
    return SKIP


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
