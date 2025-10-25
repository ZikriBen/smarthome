from settings import settings

from src.email_fetcher import fetch_latest_by_sender_scan
from src.openai_parser import extract_data_with_openai
from src.rss_manager import load_state, add_item

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
        print("[INFO] No matching email found")
        return None

    uid, from_addr, subject, body, iso_dt = result

    # Check if already processed
    state = load_state()
    if state.get("last_uid") == uid:
        print(f"[INFO] Email {uid} processed")
        return None

    print(f"[INFO] Processing new email from {from_addr}: {subject}")

    # Extract/parse data with OpenAI
    parsed_summary = extract_data_with_openai(body)

    # Add to RSS feed
    added = add_item(uid, from_addr, subject, parsed_summary, iso_dt)

    if added:
        print(f"[INFO] Added new feed item: {subject}")
        return {
            "uid": uid,
            "from": from_addr,
            "subject": subject,
            "published": iso_dt,
        }

    return None
