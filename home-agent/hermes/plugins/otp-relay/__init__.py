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
must be allowlisted and the text must yield exactly ONE code: the WebOTP line `@<domain> #<code>` when present,
else exactly one distinct standalone code (repeats of the same value are fine). The raw text is never
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
def _standalone(digits):
    """`digits` = 6 or a range "5-6" (Hapoalim sends 5 or 6 digits depending on the channel)."""
    lo, _, hi = str(digits).partition("-")
    quant = f"{{{lo},{hi or lo}}}"
    return re.compile(rf"(?<![0-9])(?<![0-9]{_SEP})([0-9]{quant})(?![0-9])(?!{_SEP}[0-9])")


SITE_CODE_RE = {
    "super_hist": _standalone(6),
    # Placeholder for the allowlist; the real rule is built per message by _hapoalim_rules() (configurable length).
    "hapoalim": _standalone("5-6"),
    # Placeholder too; the real rule is built by _cal_rules() (OTP_CAL_CODE_DIGITS, default 6).
    "cal": _standalone(6),
}

# ---------------------------------------------------------------------------------------------- Bank Hapoalim
# Only the bank's LOGIN SMS may feed a code; anything that looks like a transfer/payment/beneficiary/card approval
# code is dropped, because a relayed approval code would be a far bigger risk than a login code.
# UNVERIFIED: the real wording of the Hapoalim login SMS is not known yet. These defaults are deliberately strict
# (a false reject only means the run ends with OTP_TIMEOUT); tune them with OTP_RELAY_DEBUG_SHAPE=1 and the env
# overrides below, never by loosening the reject list.
# Real wording still UNVERIFIED (the live PoC code was typed by hand). Default since 2026-10-22: the bank name OR a
# login/identification word (the phone rule already limits the forward to the bank's sender); REJECT always wins.
HAPOALIM_REQUIRE_DEFAULT = r"הפועלים|פועלים|Poalim|POALIM|כניסה|התחברות|להתחבר|הזדהות|אימות|login|Login"
HAPOALIM_REJECT_DEFAULT = (r"העברה|העברת|להעברת|אישור פעולה|לאישור הפעולה|אישור עסקה|עסקה|תשלום|לתשלום|מוטב|"
                           r"הוראת קבע|הרשאה לחיוב|כרטיס|הלוואה|פיקדון|פקדון|משיכה|(?<![א-ת])ביט(?![א-ת])|מט.ח|"
                           r"transfer|payment|beneficiary|transaction|approve")


def _hapoalim_rules():
    """(require_re, reject_re, code_re) from env (OTP_HAPOALIM_REQUIRE_RE / _REJECT_RE / _CODE_DIGITS) or the
    strict defaults. None when the configuration is invalid -> every hapoalim message is dropped."""
    try:
        require = re.compile(os.environ.get("OTP_HAPOALIM_REQUIRE_RE", "").strip() or HAPOALIM_REQUIRE_DEFAULT, re.S)
        reject = re.compile(os.environ.get("OTP_HAPOALIM_REJECT_RE", "").strip() or HAPOALIM_REJECT_DEFAULT,
                            re.S | re.I)
        digits = os.environ.get("OTP_HAPOALIM_CODE_DIGITS", "").strip() or "5-6"
        m = re.fullmatch(r"([4-8])(?:-([4-8]))?", digits)
        if not m or (m.group(2) and int(m.group(2)) < int(m.group(1))):
            return None
    except re.error:
        return None
    return require, reject, _standalone(digits)


# ---------------------------------------------------------------------------------------------- Cal (visaCal)
# Only used if Cal ever asks for a code at login (the password login normally does not). Same stance as the bank:
# the login SMS only; a purchase / 3-D Secure / wallet-token / transaction approval code must NEVER be relayed,
# so REJECT always wins. UNVERIFIED wording: tune with OTP_RELAY_DEBUG_SHAPE=1 + OTP_CAL_REQUIRE_RE, never by
# loosening OTP_CAL_REJECT_RE.
CAL_REQUIRE_DEFAULT = r"כאל|cal-online|(?<![A-Za-z])CAL(?![A-Za-z])|(?<![A-Za-z])Cal(?![A-Za-z])|כניסה|התחברות|להתחבר|הזדהות|אימות|login|Login"
CAL_REJECT_DEFAULT = (r"עסקה|עסקת|לאישור|אישור עסקה|אישור רכישה|רכישה|קנייה|באתר|אונליין|תשלום|לתשלום|חיוב|העברה|"
                      r"הוראת קבע|הלוואה|מסגרת|ארנק|דיגיטלי|טוקן|Apple ?Pay|Google ?Pay|3-?D|3DS|secure|₪|ש\"ח|ILS|USD|"
                      r"EUR|transaction|payment|purchase|approve|wallet|token")


def _cal_rules():
    """(require_re, reject_re, code_re) from env (OTP_CAL_REQUIRE_RE / _REJECT_RE / _CODE_DIGITS) or the strict
    defaults. None when the configuration is invalid -> every cal message is dropped."""
    try:
        require = re.compile(os.environ.get("OTP_CAL_REQUIRE_RE", "").strip() or CAL_REQUIRE_DEFAULT, re.S)
        reject = re.compile(os.environ.get("OTP_CAL_REJECT_RE", "").strip() or CAL_REJECT_DEFAULT, re.S | re.I)
        digits = os.environ.get("OTP_CAL_CODE_DIGITS", "").strip() or "6"
        m = re.fullmatch(r"([4-8])(?:-([4-8]))?", digits)
        if not m or (m.group(2) and int(m.group(2)) < int(m.group(1))):
            return None
    except re.error:
        return None
    return require, reject, _standalone(digits)


# A bank SMS must never feed another site's login (a mis-tagged forwarding rule): any non-bank site drops a
# message that mentions the bank.
BANK_MENTION_RE = re.compile(r"הפועלים|פועלים|poalim|בנק", re.I)
# ...and the reverse: a Super-Hist SMS mis-tagged `#OTP hapoalim` (or a rule forwarding both) never feeds the bank.
OTHER_SITE_RE = re.compile(r"super-hist|superhist|הסופר החברתי|הסופר|ההסתדרות|histadrut", re.I)


def guard_reason(site, body):
    """None when `body` may feed a code for `site`; else a digit-free reason to drop it."""
    if site == "cal":
        rules = _cal_rules()
        if rules is None:
            return "invalid cal pattern configuration"
        if OTHER_SITE_RE.search(body or "") or BANK_MENTION_RE.search(body or ""):
            return "mentions another site (bank/super_hist); not a Cal SMS"
        require, reject, _ = rules
        if reject.search(body):
            return "looks like a purchase/approval SMS, not a login SMS"
        if not require.search(body):
            return "not Cal's login SMS wording"
        return None
    if site != "hapoalim":
        return "mentions the bank; not a %s SMS" % site if BANK_MENTION_RE.search(body or "") else None
    rules = _hapoalim_rules()
    if rules is None:
        return "invalid hapoalim pattern configuration"
    if OTHER_SITE_RE.search(body or ""):
        return "mentions another site (super_hist); not a bank SMS"
    require, reject, _ = rules
    if reject.search(body):
        return "looks like a transfer/approval SMS, not a login SMS"
    if not require.search(body):
        return "not the bank's login SMS wording"
    return None
# WebOTP / SMS-retriever origin-bound line (`@<domain> #<code>` on its own line), the authoritative code when the
# SMS carries one. The real Super-Hist SMS is "<code> הוא קוד הכניסה החד פעמי לאתר\n@super-hist.co.il #<code>".
SITE_WEBOTP_RE = {
    "super_hist": re.compile(r"^@super-hist\.co\.il #([0-9]{6})[ \t\r]*$", re.M),
    # UNVERIFIED: whether the bank's SMS carries a WebOTP line at all.
    "hapoalim": re.compile(r"^@(?:[a-z0-9-]+\.)*bankhapoalim\.co\.il #([0-9]{4,8})[ \t\r]*$", re.M),
    # UNVERIFIED as well.
    "cal": re.compile(r"^@(?:[a-z0-9-]+\.)*cal-online\.co\.il #([0-9]{4,8})[ \t\r]*$", re.M),
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
    """(code or None, stats, reason). Rules, in order:
    1. a WebOTP line (`@super-hist.co.il #<code>`) with exactly one distinct value wins; if the free text also
       has a DIFFERENT code, the WebOTP value is still used (it is origin-bound) and the reason says so;
       two distinct WebOTP values -> reject;
    2. otherwise all standalone candidates are de-duplicated: exactly one distinct value -> accept,
       none -> reject, two or more distinct values -> reject.
    `stats` = (raw candidate count, distinct values, WebOTP lines). `reason` never contains digits."""
    rule = SITE_CODE_RE.get(site)
    if site == "hapoalim":
        rules = _hapoalim_rules()
        rule = rules[2] if rules else None
    elif site == "cal":
        rules = _cal_rules()
        rule = rules[2] if rules else None
    if rule is None or not body:
        return None, (0, 0, 0), "no code found"
    found = rule.findall(body)
    distinct = set(found)
    web_rule = SITE_WEBOTP_RE.get(site)
    web = web_rule.findall(body) if web_rule is not None else []
    distinct |= set(web)
    stats = (len(found), len(distinct), len(web))
    if web:
        if len(set(web)) != 1:
            return None, stats, "conflicting WebOTP lines"
        code = web[0]
        if distinct - {code}:
            return code, stats, "WebOTP line used; the text has another, different code"
        return code, stats, None
    if not distinct:
        return None, stats, "no code found"
    if len(distinct) > 1:
        return None, stats, "several distinct codes, need exactly one"
    return next(iter(distinct)), stats, None


def _log_shape(text, stats=(-1, -1, -1)):
    if os.environ.get("OTP_RELAY_DEBUG_SHAPE", "").strip() != "1":
        return
    masked = re.sub(r"\d", "*", text)            # mask FIRST; nothing below sees a digit
    shape = masked[:300].replace("\r", "").replace("\n", "⏎")
    matches, unique, webotp = stats
    logger.info("otp-relay shape: len=%d lines=%d matches=%d unique=%d webotp=%d text=%r", len(text),
                masked.count("\n") + 1, matches, unique, webotp, shape)


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
        _log_shape(text)
        logger.info("otp-relay: dropped a forward-bot message (too long)")
        return True
    match = MESSAGE_RE.fullmatch(text)
    if match is None:
        _log_shape(text)
        logger.info("otp-relay: dropped a forward-bot message (not '#OTP <site> <sms>')")
        return True
    site, body = match.group(1), match.group(2) or ""
    if site not in SITE_CODE_RE or site not in otp_inbox.SITES:
        _log_shape(text)
        logger.info("otp-relay: dropped a forward-bot message (unknown site)")
        return True
    blocked = guard_reason(site, body)
    if blocked:
        _log_shape(text)
        del text, body
        logger.info("otp-relay: dropped a %s message (%s)", site, blocked)
        return True
    code, stats, reason = extract_code(site, body)
    _log_shape(text, stats)
    del text, body
    if code is None:
        logger.info("otp-relay: dropped a %s message (%s)", site, reason)
        return True
    if reason:
        logger.info("otp-relay: %s (%s)", site, reason)
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
