"""Jellyfin 12.1 rejects X-Emby-Token (401). Every call site must use
``Authorization: MediaBrowser Token="..."`` and never put a key in a URL."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(r"X-Emby-Token|X-MediaBrowser-Token|[?&]api_key=|[?&]ApiKey=|\"api_key\"\s*:|'api_key'\s*:", re.I)


def _sources():
    for sub in ("telegram-browser/app", "telegram-downloader/app"):
        yield from (ROOT / sub).rglob("*.py")


def test_no_legacy_jellyfin_auth_anywhere():
    hits = [f"{p.relative_to(ROOT)}:{i}" for p in _sources()
            for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1) if FORBIDDEN.search(line)]
    assert hits == []


def test_jellyfin_call_sites_use_authorization_header():
    for rel in ("telegram-browser/app/stream/watch.py", "telegram-downloader/app/jellyfin.py"):
        text = (ROOT / rel).read_text()
        assert "MediaBrowser Token=" in text and "Authorization" in text, rel


def test_browser_has_a_single_jellyfin_client():
    # all browser Jellyfin traffic goes through watch.JellyfinClient._request
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "telegram-browser/app").rglob("*.py")
                 if "MediaBrowser Token" in p.read_text() and p.name != "watch.py"]
    assert offenders == []
