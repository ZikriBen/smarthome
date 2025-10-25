import re
from imap_tools import MailBox, AND # pyright: ignore[reportPrivateImportUsage]
from datetime import timezone
from typing import Optional, Tuple

def _to_plain_text(text: str | None, html: str | None) -> str:
    if text and text.strip():
        return text
    if not html:
        return ""
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)<script.*?>.*?</script>", "", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", "", html)
    return re.sub(r"(?s)<[^>]+>", "", html).strip()

def fetch_latest_by_sender_scan(
    host: str,
    user: str,
    password: str,
    mailbox: str = "INBOX",
    sender: str = "",
    exact: bool = True,
    limit: int = 50,
) -> Optional[Tuple[str, str, str, str, str]]:
    """
    Scan the newest `limit` messages and return the first whose sender
    matches the query (exact or substring match on email address).

    Args:
        host: IMAP server host
        user: Email username
        password: Email password
        mailbox: Mailbox name (default: "INBOX")
        sender: Email address or part of it to match
        exact: If True, exact match; if False, substring match
        limit: Number of recent emails to scan

    Returns: (uid, from_address, subject, body_text, iso_datetime_utc) or None
    """
    sender_q = (sender or "").strip().lower()

    with MailBox(host).login(user, password, mailbox) as m:
        # Fetch newest first
        for msg in m.fetch(AND(all=True), reverse=True, limit=limit):
            from_addr = (msg.from_ or "").strip().lower()

            if exact:
                if from_addr != sender_q:
                    continue
            else:
                if sender_q not in from_addr:
                    continue

            body = _to_plain_text(msg.text, msg.html)
            dt = msg.date
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            iso_dt = dt.astimezone(timezone.utc).isoformat()

            return str(msg.uid), msg.from_, msg.subject or "", body, iso_dt

    return None