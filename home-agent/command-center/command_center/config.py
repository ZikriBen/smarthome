"""Runtime configuration shared by Command Center integrations."""

import os
from pathlib import Path

TOKEN = os.environ["COMMAND_CENTER_TOKEN"]
CALLBACK_TOKEN = os.environ.get("COMMAND_CENTER_CALLBACK_TOKEN", "")

KUMA_URL = os.environ.get("UPTIME_KUMA_URL", "").rstrip("/")
KUMA_API_KEY = os.environ.get("UPTIME_KUMA_API_KEY", "")
BROWSER_URL = os.environ.get("TELEGRAM_BROWSER_URL", "").rstrip("/")
DOWNLOADER_URL = os.environ.get("TELEGRAM_DOWNLOADER_URL", "").rstrip("/")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

DATABASE_PATH = Path("/data/command-center.sqlite3")
ATTACHMENTS_PATH = Path("/attachments").resolve()
WORKSPACE_PATH = Path("/workspace").resolve()
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024

GOOGLE_CLIENT_SECRET = Path("/google/client-secret.json")
GOOGLE_TOKEN = Path("/data/google-token.json")
GOOGLE_REDIRECT_URI = "http://localhost:8766/"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events.owned",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
