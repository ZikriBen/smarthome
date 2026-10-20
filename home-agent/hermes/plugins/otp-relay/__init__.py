"""otp-relay: take one-time SMS codes posted by Ben's forward bot out of the Telegram stream BEFORE auth and
before the agent, and drop ONLY the code into the shared OTP inbox for the Command Center.

Two entry points share one core (`consume`):
  * the Telegram adapter's early intercept (hermes/telegram-adapter.py, `_otp_relay_intercept`), which runs
    before the adapter's own auth gate — the forward bot is NOT an allowed user, so without it the message is
    dropped as "Blocked unauthorized user" and never reaches the gateway hook;
  * the gateway `pre_gateway_dispatch` hook (defense in depth for any path that reaches the gateway).

Every message from the forward bot (OTP_FORWARD_BOT_ID) is consumed and never reaches auth, the session
transcript, or the LLM. Only a message in its PRIVATE chat (OTP_FORWARD_CHAT_ID, default = the bot's own id)
whose text is `#OTP <site>` + whitespace + free text (the raw SMS, multiline allowed) is considered; the site
must be allowlisted and the text must contain exactly ONE standalone code for that site. The raw text is never
logged or stored, only the code is stored. With OTP_FORWARD_BOT_ID unset the plugin is inert.
Optional OTP_RELAY_DEBUG_SHAPE=1 logs the payload SHAPE (all digits masked, length, lines, match count).
"""

import logging
import os
import re

try:
    from . import otp_inbox
except ImportError:  # loaded as a plain module (tests)
    import otp_inbox  # type: ignore

logger = logging.getLogger(__name__)
# `#OTP <site>` then at least one whitespace (space/newline) and the free text; nothing before `#OTP`.
MESSAGE_RE = re.compile(r"#OTP ([a-z_]{1,32})(?:\s+(.*))?", re.S)
MAX_TEXT = 2000
SKIP = {"action": "skip", "reason": "otp-relay"}
# A code is a run of ASCII digits not touching another digit, nor a digit across a separator (phone numbers
# like 03-123456, amounts like 123456.78, times, dates). Hebrew letters/punctuation next to it are fine.
_SEP = r"[.,:/\-]"
SITE_CODE_RE = {
    "super_hist": re.compile(rf"(?<![0-9])(?<![0-9]{_SEP})([0-9]{{6}})(?![0-9])(?!{_SEP}[0-9])"),
}


def _config():
    bot = os.environ.get("OTP_FORWARD_BOT_ID", "").strip()
    chat = os.environ.get("OTP_FORWARD_CHAT_ID", "").strip() or bot
    path = os.environ.get("OTP_INBOX_DB", "/opt/data/otp/codes.sqlite")
    return bot, chat, path


def is_forward_bot(user_id):
    bot = _config()[0]
    return bot.isdigit() and str(user_id if user_id is not None else "") == bot


def extract_code(site, body):
    """(code or None, number of candidate codes). Exactly one candidate is required."""
    rule = SITE_CODE_RE.get(site)
    if rule is None or not body:
        return None, 0
    found = rule.findall(body)
    return (found[0] if len(found) == 1 else None), len(found)


def _log_shape(text, matches):
    if os.environ.get("OTP_RELAY_DEBUG_SHAPE", "").strip() != "1":
        return
    masked = re.sub(r"\d", "*", text)            # mask FIRST; nothing below sees a digit
    shape = masked[:300].replace("\r", "").replace("\n", "⏎")
    logger.info("otp-relay shape: len=%d lines=%d matches=%d text=%r", len(text), masked.count("\n") + 1,
                matches, shape)


def consume(user_id, chat_id, is_private, text, message_id=None):
    """Core for both entry points. None = not from the forward bot (not ours, untouched).
    True = consumed (caller MUST drop the message), whether or not a code was stored."""
    bot, chat, path = _config()
    if not bot.isdigit() or str(user_id if user_id is not None else "") != bot:
        return None
    text = str(text or "").strip()
    if not is_private or str(chat_id if chat_id is not None else "") != chat:
        logger.info("otp-relay: dropped a forward-bot message (not its allowlisted private chat)")
        return True
    if len(text) > MAX_TEXT:
        _log_shape(text, -1)
        logger.info("otp-relay: dropped a forward-bot message (too long)")
        return True
    match = MESSAGE_RE.fullmatch(text)
    if match is None:
        _log_shape(text, -1)
        logger.info("otp-relay: dropped a forward-bot message (not '#OTP <site> <sms>')")
        return True
    site, body = match.group(1), match.group(2) or ""
    if site not in SITE_CODE_RE or site not in otp_inbox.SITES:
        _log_shape(text, -1)
        logger.info("otp-relay: dropped a forward-bot message (unknown site)")
        return True
    code, count = extract_code(site, body)
    _log_shape(text, count)
    del text, body
    if code is None:
        logger.info("otp-relay: dropped a %s message (%s)", site,
                    "no code found" if count == 0 else f"{count} candidate codes, need exactly one")
        return True
    try:
        stored = otp_inbox.put(path, site, code, ref=f"tg:{chat}:{message_id}" if message_id else None)
    except Exception as exc:  # never let a store problem leak the message to the agent
        logger.warning("otp-relay: inbox write failed (%s)", type(exc).__name__)
        return True
    finally:
        del code
    logger.info("otp-relay: %s code for %s", "stored" if stored else "ignored duplicate/invalid", site)
    return True


def _platform_name(source):
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "").lower()


def on_pre_gateway_dispatch(event=None, **_kwargs):
    source = getattr(event, "source", None)
    if source is None or _platform_name(source) != "telegram":
        return None
    is_private = str(getattr(source, "chat_type", "") or "").lower() in ("dm", "private")
    message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
    out = consume(getattr(source, "user_id", None), getattr(source, "chat_id", None), is_private,
                  getattr(event, "text", ""), message_id)
    return SKIP if out else None


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
