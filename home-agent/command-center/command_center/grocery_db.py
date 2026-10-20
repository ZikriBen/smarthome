"""SQLite tables for groceries (additive, CREATE TABLE IF NOT EXISTS in /data/command-center.sqlite3).

grocery_runs        one row per cart job (status, final message, links)
grocery_run_lines   what was picked / linked per site (feeds pins, "same as last time", regulars)
grocery_learned     audit of every learn call (what Ben said and when)
grocery_site_state  per-site circuit breaker (blocked_until)
"""

import json
import threading
import time

BLOCK_SECONDS = 24 * 3600
RUN_COLUMNS = {"sites": "TEXT", "message": "TEXT", "links_json": "TEXT", "ordered": "INTEGER DEFAULT 0",
               "extra_json": "TEXT"}   # extra_json: {needs_choice, unmatched_choices} (additive column)
_db_lock = threading.Lock()


def init(conn):
    with _db_lock:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS grocery_runs (id TEXT PRIMARY KEY, site TEXT, sites TEXT, "
            "status TEXT NOT NULL, started INTEGER NOT NULL, finished INTEGER, summary TEXT, message TEXT, "
            "links_json TEXT, error TEXT, ordered INTEGER DEFAULT 0)")
        have = {r[1] for r in conn.execute("PRAGMA table_info(grocery_runs)")}
        for col, typ in RUN_COLUMNS.items():           # an older grocery_runs table (never shipped) gets the columns
            if col not in have:
                conn.execute(f"ALTER TABLE grocery_runs ADD COLUMN {col} {typ}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS grocery_run_lines (run_id TEXT NOT NULL, site TEXT NOT NULL, "
            "item TEXT, qty REAL, qty_unit TEXT, product_id TEXT, link_code TEXT, product_name TEXT, "
            "price REAL, unit_price REAL, confidence TEXT, flags TEXT, in_link INTEGER)")
        conn.execute("CREATE INDEX IF NOT EXISTS grocery_run_lines_run ON grocery_run_lines(run_id)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS grocery_learned (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
            "item TEXT NOT NULL, change_json TEXT, reason TEXT, source TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS grocery_site_state (site TEXT PRIMARY KEY, blocked_until INTEGER, "
            "last_error TEXT, last_ok INTEGER)")
        # A job interrupted by a restart must not stay "running" forever.
        conn.execute("UPDATE grocery_runs SET status='failed', error='interrupted by restart' WHERE status='running'")
        conn.commit()


def start_run(conn, ident, sites):
    with _db_lock:
        conn.execute("INSERT INTO grocery_runs(id,site,sites,status,started) VALUES (?,?,?,?,?)",
                     (ident, "cart", ",".join(sites), "running", int(time.time())))
        conn.commit()


def finish_run(conn, ident, status, message=None, links=None, error=None, lines=None, extra=None):
    """lines: {site: [plan line dicts]} (POC plan format) + in_link from link_code presence."""
    with _db_lock:
        conn.execute("UPDATE grocery_runs SET status=?, finished=?, message=?, summary=?, links_json=?, error=?, "
                     "extra_json=? WHERE id=?", (status, int(time.time()), message, message,
                                                 json.dumps(links or {}, ensure_ascii=False), error,
                                                 json.dumps(extra, ensure_ascii=False) if extra else None, ident))
        for site, ls in (lines or {}).items():
            for l in ls:
                conn.execute(
                    "INSERT INTO grocery_run_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ident, site, l.get("item"), l.get("qty"), "kg" if l.get("by_weight") else "unit",
                     l.get("product_id"), l.get("link_code"), l.get("product_name"), l.get("price"),
                     l.get("unit_price"), l.get("confidence"), json.dumps(l.get("flags") or [], ensure_ascii=False),
                     int(bool(l.get("link_code")) and not l.get("to_check") and l.get("price") is not None)))
        conn.commit()


def get_run(conn, ident):
    row = conn.execute("SELECT sites,status,started,finished,message,links_json,error,extra_json FROM grocery_runs "
                       "WHERE id=?", (ident,)).fetchone()
    if row is None:
        return None
    keys = ("sites", "status", "started", "finished", "message", "links_json", "error", "extra_json")
    return dict(zip(keys, row))


def run_lines(conn, ident):
    cur = conn.execute("SELECT site,item,product_id,link_code,price,in_link FROM grocery_run_lines WHERE run_id=?",
                       (ident,))
    return [dict(zip(("site", "item", "product_id", "link_code", "price", "in_link"), r)) for r in cur]


def last_pick(conn, item, site):
    """product_id of the most recent finished pick of <item> on <site> (for "תמיד את זה" pins)."""
    row = conn.execute(
        "SELECT l.product_id FROM grocery_run_lines l JOIN grocery_runs r ON r.id=l.run_id "
        "WHERE l.item=? AND l.site=? AND l.product_id IS NOT NULL AND r.status='done' "
        "ORDER BY r.started DESC LIMIT 1", (item, site)).fetchone()
    return row[0] if row else None


def mark_last_ordered(conn):
    with _db_lock:
        row = conn.execute("SELECT id FROM grocery_runs WHERE status='done' ORDER BY started DESC LIMIT 1").fetchone()
        if row:
            conn.execute("UPDATE grocery_runs SET ordered=1 WHERE id=?", (row[0],))
            conn.commit()
        return row[0] if row else None


def log_learned(conn, item, change, reason, source="hermes"):
    with _db_lock:
        conn.execute("INSERT INTO grocery_learned(ts,item,change_json,reason,source) VALUES (?,?,?,?,?)",
                     (int(time.time()), item, json.dumps(change, ensure_ascii=False), reason or "", source))
        conn.commit()


def blocked(conn, site, now=None):
    """(blocked_until, last_error) if the site is still blocked, else None."""
    row = conn.execute("SELECT blocked_until,last_error FROM grocery_site_state WHERE site=?", (site,)).fetchone()
    now = now if now is not None else time.time()
    if row and row[0] and row[0] > now:
        return row[0], row[1]
    return None


def set_blocked(conn, site, error, now=None):
    now = int(now if now is not None else time.time())
    with _db_lock:
        conn.execute("INSERT INTO grocery_site_state(site,blocked_until,last_error) VALUES (?,?,?) "
                     "ON CONFLICT(site) DO UPDATE SET blocked_until=excluded.blocked_until, "
                     "last_error=excluded.last_error", (site, now + BLOCK_SECONDS, (error or "")[:300]))
        conn.commit()


def set_ok(conn, site, now=None):
    now = int(now if now is not None else time.time())
    with _db_lock:
        conn.execute("INSERT INTO grocery_site_state(site,last_ok) VALUES (?,?) "
                     "ON CONFLICT(site) DO UPDATE SET last_ok=excluded.last_ok", (site, now))
        conn.commit()
