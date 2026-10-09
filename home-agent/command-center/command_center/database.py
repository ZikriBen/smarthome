"""SQLite state owned by the Command Center process."""

import sqlite3
import threading

from .config import DATABASE_PATH, WORKSPACE_PATH

DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)

connection = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
connection.execute(
    "CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, action TEXT NOT NULL, "
    "payload TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL)"
)
connection.execute(
    "CREATE TABLE IF NOT EXISTS watches (id TEXT PRIMARY KEY, url TEXT NOT NULL, "
    "target REAL NOT NULL, currency TEXT NOT NULL, every_minutes INTEGER NOT NULL, "
    "enabled INTEGER NOT NULL, last_price REAL, last_checked INTEGER)"
)
connection.execute(
    "CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "watch_id TEXT NOT NULL, price REAL NOT NULL, created INTEGER NOT NULL)"
)
connection.execute(
    "CREATE TABLE IF NOT EXISTS searchgram_sessions (id TEXT PRIMARY KEY, query TEXT NOT NULL, "
    "page_json TEXT NOT NULL, created INTEGER NOT NULL)"
)
connection.commit()

# Approval claims must be atomic across ThreadingHTTPServer request threads.
approval_lock = threading.Lock()
