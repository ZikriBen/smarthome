import logging

from settings import settings
from src.email_fetcher import fetch_latest_by_sender_scan
from src.determinstic_parser import extract_halachot, ExtractionError
from src.rss_manager import load_state, add_item

logger = logging.getLogger(__name__)

def poll_once():
    """Check for new email and add to RSS feed if found."""
    # Fetch latest email by sender
    result = fetch_latest_by_sender_scan(
        host=settings.IMAP_HOST,
        user=settings.IMAP_USER,
        password=settings.IMAP_PASS,
        mailbox=settings.IMAP_MAILBOX,
        sender=settings.EMAIL_SENDER,
        exact=settings.EXACT_MATCH,
    )

    if not result:
        logger.info("No matching email found")
        return None

    uid, from_addr, subject, body, iso_dt = result

    # Check if already processed
    state = load_state()
    if state.get("last_uid") == uid:
        logger.info(f"Email {uid} processed")
        return None

    logger.info(f"Processing new email from {from_addr}: {subject}")

    # Extract the halachot deterministically
    try:
        halachot = extract_halachot(body)
    except ExtractionError as e:
        logger.warning(f"Email {uid} did not match expected format: {e}")
        return None

    # Add to RSS feed: keep the date subject and append the halachot
    # heading, so the title still shows which day this is for.
    title = f"{subject} - {halachot.heading}"
    added = add_item(uid, from_addr, title, halachot.body, iso_dt)

    if added:
        logger.info(f"Added new feed item: {subject}")
        return {
            "uid": uid,
            "from": from_addr,
            "subject": subject,
            "published": iso_dt,
        }

    return None
