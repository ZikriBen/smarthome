"""Tiny one-time-code inbox shared by the Hermes `otp-relay` plugin (writer) and the Command Center (reader).

One SQLite file in a directory only these two containers mount (host: home-agent/hermes/otp, 0700, file 0600).
Retention: a code lives at most TTL seconds, at most MAX_ROWS rows exist, and reading one code deletes every code
for that site (single use). Codes are never logged. This file is duplicated byte-for-byte in
hermes/plugins/otp-relay/otp_inbox.py (a test enforces that the copies stay identical).
"""

import os
import re
import sqlite3
import time
from pathlib import Path

TTL = 600
MAX_ROWS = 5
SITES = {"super_hist": re.compile(r"\d{6}")}
SCHEMA = ("CREATE TABLE IF NOT EXISTS otp_codes (id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT NOT NULL, "
          "code TEXT NOT NULL, received REAL NOT NULL, ref TEXT UNIQUE)")


def _connect(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5)
    con.execute(SCHEMA)
    os.chmod(path, 0o600)
    return con


def _purge(con, now):
    con.execute("DELETE FROM otp_codes WHERE received < ? OR received > ?", (now - TTL, now + 60))
    con.execute("DELETE FROM otp_codes WHERE id NOT IN (SELECT id FROM otp_codes ORDER BY id DESC LIMIT ?)",
                (MAX_ROWS,))


def valid(site, code):
    rule = SITES.get(site)
    return bool(rule and isinstance(code, str) and rule.fullmatch(code))


def put(path, site, code, ref=None, now=None):
    """Store one code. False (nothing written) for an unknown site, a malformed code, or a duplicate ref."""
    if not valid(site, code):
        return False
    now = time.time() if now is None else now
    con = _connect(path)
    try:
        with con:
            cur = con.execute("INSERT OR IGNORE INTO otp_codes(site,code,received,ref) VALUES (?,?,?,?)",
                              (site, code, now, ref))
            _purge(con, now)
        return cur.rowcount == 1
    finally:
        con.close()


def take(path, site, since=0.0, now=None):
    """Return the newest unexpired code for `site` received at or after `since`, and delete all codes for
    `site` (single use). None when there is none."""
    now = time.time() if now is None else now
    if not Path(path).parent.is_dir():
        return None
    con = _connect(path)
    try:
        with con:
            _purge(con, now)
            row = con.execute("SELECT code FROM otp_codes WHERE site=? AND received>=? ORDER BY received DESC, "
                              "id DESC LIMIT 1", (site, since)).fetchone()
            if row is not None:
                con.execute("DELETE FROM otp_codes WHERE site=?", (site,))
        return row[0] if row else None
    finally:
        con.close()


def pending(path, now=None):
    """[(site, age_seconds)] of unexpired codes, without the codes themselves."""
    now = time.time() if now is None else now
    if not Path(path).exists():
        return []
    con = _connect(path)
    try:
        with con:
            _purge(con, now)
            return [(site, int(now - received)) for site, received in
                    con.execute("SELECT site,received FROM otp_codes ORDER BY id")]
    finally:
        con.close()
