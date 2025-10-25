import os
import json
from feedgen.feed import FeedGenerator
from settings import settings

def load_state():
    path = settings.STATE_FILE
    if not os.path.exists(path):
        return {"last_uid": None, "items": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    path = settings.STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def add_item(uid: str, from_addr: str, subject: str, summary: str, published: str):
    """Add a new item to the RSS feed."""
    state = load_state()

    # Check if already processed
    if state.get("last_uid") == uid:
        return False

    item = {
        "guid": uid,
        "title": subject or f"Email from {from_addr}",
        "link": settings.RSS_LINK,
        "summary": summary,
        "published": published,
        "from": from_addr,
    }

    # Add new item and keep only MAX_ITEMS
    items = [item] + state.get("items", [])
    state["items"] = items[:settings.MAX_ITEMS]
    state["last_uid"] = uid
    save_state(state)
    return True

def build_rss():
    """Generate RSS XML from current state."""
    state = load_state()
    fg = FeedGenerator()
    fg.title(settings.RSS_TITLE)
    fg.link(href=settings.RSS_LINK, rel="alternate")
    fg.description("Auto feed from email parser")
    fg.language("he")  # Set Hebrew language

    for it in state.get("items", []):
        fe = fg.add_entry()
        fe.id(it["guid"])
        fe.title(it["title"])
        fe.link(href=it["link"])
        fe.description(it["summary"])
        fe.published(it["published"])

    return fg.rss_str(pretty=True, encoding='utf-8')

def get_state():
    """Get current state for status checks."""
    return load_state()

