"""Grocery list (Google Tasks "קניות"), learned preferences and the "תכין עגלה" cart job.

The working list lives in Google Tasks (see grocery_list.py); state/current_list.json is only its cache.
The cart job searches Rami Levy and Shufersal READ-ONLY (in parallel; Rami 1.5 s, Shufersal >= 10 s between
requests) and returns cart LINKS that Ben opens in his own browser. Nothing here logs in to a supermarket,
writes to a supermarket cart or checks out; the vendored `grocery` package has that code stubbed out.
"""

import os
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import grocery_db, grocery_list
from .config import GROCERY_LIST_BACKEND
from .database import connection

IMAGE_DEFAULTS = Path(os.environ.get("GROCERY_IMAGE_DEFAULTS", "/app/grocery-defaults.yaml"))
DATA_DIR = Path(os.environ.get("GROCERY_DATA_DIR", "/data/grocery"))
DEFAULTS = DATA_DIR / "defaults.yaml"            # refreshed from the image on every start
USER_DEFAULTS = DATA_DIR / "defaults.user.yaml"  # learned; persistent, never overwritten
STATE_DIR = DATA_DIR / "state"
REPORTS_DIR = Path(os.environ.get("GROCERY_REPORTS_DIR", "/workspace/grocery"))
CART_SITES = ("rami_levy", "shufersal")
SITE_NAMES = {"rami_levy": "רמי לוי", "shufersal": "שופרסל"}
STALE_SECONDS = 30 * 60
ETA_SECONDS = 180
POLL_SECONDS = 2
FIXTURES = os.environ.get("GROCERY_FIXTURES_ROOT") or None   # tests / dry runs only; unset in production

_lock = threading.Lock()       # list/defaults file writes
_run_lock = threading.Lock()   # one cart job at a time (polite to the sites)


def log(message):
    print(f"[grocery] {message}", flush=True)


def _api():
    from grocery import api  # vendored package; imported lazily so startup never breaks on it
    return api


def setup():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["GROCERY_STATE_DIR"] = str(STATE_DIR)
    if IMAGE_DEFAULTS.is_file():
        shutil.copyfile(IMAGE_DEFAULTS, DEFAULTS)
    grocery_list.configure(DEFAULTS, STATE_DIR / "current_list.json", GROCERY_LIST_BACKEND)
    grocery_db.init(connection)


def _text(fn, *args, **kwargs):
    with _lock:
        return {"text": fn(*args, **kwargs)}


def add_item(p, _q):
    return _text(grocery_list.add_item, str(p["text"]))


def list_items(_p, _q):
    return _text(grocery_list.list_items)


def remove_item(p, _q):
    return _text(grocery_list.remove_item, p["item"])


def clear(p, _q):
    undo = bool(p.get("undo", False))
    out = _text(grocery_list.clear, undo)
    if not undo:
        grocery_db.mark_last_ordered(connection)   # "הזמנתי" -> clear: the last cart run counts as ordered
    return out


def sync(_p, _q):
    return _text(grocery_list.sync_tool)


def _resolve_pin(item, change, site):
    """{"pin": {site: ""}} / {"pin": "last"} -> the product id of the last pick of <item> on that site."""
    if not isinstance(change, dict) or "pin" not in change:
        return change
    pin = change["pin"]
    if isinstance(pin, dict) and all(pin.values()):
        return change
    sites = [s for s, v in pin.items() if not v] if isinstance(pin, dict) else ([site] if site else [])
    if not sites:
        raise ValueError("pin needs a site (rami_levy / shufersal) or {site: product_id}")
    names = [item]
    try:
        from grocery.defaults import load_defaults
        canon = load_defaults(str(DEFAULTS)).lookup(item)
        if canon:
            names.insert(0, canon)
    except Exception:
        pass
    resolved = dict(pin) if isinstance(pin, dict) else {}
    for s in sites:
        pid = next((x for x in (grocery_db.last_pick(connection, n, s) for n in names) if x), None)
        if not pid:
            raise ValueError(f"no previous pick of {item} on {s} to pin; run grocery_cart first or pass the id")
        resolved[s] = pid
    return {**change, "pin": resolved}


def learn(p, _q):
    item, site = str(p["item"]), p.get("site") or None
    change = _resolve_pin(item, p["change"], site)
    out = _text(_api().learn_preference, item, change, p.get("reason", ""), site, defaults_path=str(DEFAULTS))
    if out["text"].startswith("נלמד"):
        grocery_db.log_learned(connection, item, change, p.get("reason", ""))
    return out


def _skipped(sites, force=False):
    out = {}
    for s in sites:
        b = grocery_db.blocked(connection, s)
        if b:
            until = time.strftime("%d.%m %H:%M", time.localtime(b[0]))
            out[s] = f"נחסם לאחרונה, לא שולחת בקשות עד {until}"
    return out


def _cart_job(ident, sites, skipped, warning=None):
    try:
        r = _api().cart(sites, fixtures=FIXTURES, out_dir=str(REPORTS_DIR), defaults_path=str(DEFAULTS),
                        log=lambda m: log(f"cart {ident} {m}"), skipped=skipped)
        for s, err in r.get("blocked", {}).items():
            grocery_db.set_blocked(connection, s, err)
            log(f"cart {ident} {s} BLOCKED for 24h: {err}")
        for s in r.get("plan_paths", {}):
            if s not in r.get("site_errors", {}):
                grocery_db.set_ok(connection, s)
        message = r["message"]
        if warning:
            message = f"{warning}\n{message}"
        grocery_db.finish_run(connection, ident, "done", message, r["links"], None, r.get("lines"))
        log(f"cart {ident} done items={r['items']} links={ {s: bool(u) for s, u in r['links'].items()} }")
    except Exception as exc:  # recorded AND logged; never swallowed (price_watches lesson)
        error = f"{type(exc).__name__}: {exc}"
        log(f"cart {ident} failed: {error}\n{traceback.format_exc()}")
        grocery_db.finish_run(connection, ident, "failed", None, None, error)
    finally:
        _run_lock.release()


def start_cart(p, _q):
    sites = p.get("sites", "both") or "both"
    want = list(CART_SITES) if sites == "both" else [sites]
    if any(s not in CART_SITES for s in want):
        raise ValueError("sites must be both, rami_levy or shufersal")
    if not _run_lock.acquire(blocking=False):
        raise ValueError("a grocery cart job is already running; check grocery_cart_status")
    try:
        with _lock:   # pull Tasks now (ticked items drop out); the job reads the fresh cache
            st, warning, _n = grocery_list.sync()
        if not st["items"]:
            _run_lock.release()
            return {"run_id": None, "status": "done", "message": "הרשימה ריקה — אין מה לבנות",
                    "links": {}, "eta_seconds": 0}
        skipped = _skipped(want)
        ident = os.urandom(6).hex()
        grocery_db.start_run(connection, ident, want)
    except Exception:
        if _run_lock.locked():
            _run_lock.release()
        raise
    threading.Thread(target=_cart_job, args=(ident, want, skipped, warning), daemon=True).start()
    eta = 0 if len(skipped) == len(want) else ETA_SECONDS
    result = {"run_id": ident, "status": "running", "eta_seconds": eta, "items": len(st["items"])}
    if skipped:
        result["skipped"] = {s: w for s, w in skipped.items()}
    if warning:
        result["warning"] = warning
    return result


def cart_status(p, _q):
    ident = str(p["run_id"])
    deadline = time.time() + max(0, min(int(p.get("wait_seconds", 0)), 90))
    while True:
        row = grocery_db.get_run(connection, ident)
        if row is None:
            raise ValueError("unknown grocery cart run")
        if row["status"] != "running" or time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    status, error = row["status"], row["error"]
    if status == "running" and time.time() - row["started"] > STALE_SECONDS:
        status, error = "failed", "run exceeded 30 minutes"
    import json
    links = json.loads(row["links_json"]) if row["links_json"] else {s: None for s in row["sites"].split(",")}
    return {"run_id": ident, "status": status, "seconds": (row["finished"] or int(time.time())) - row["started"],
            "message": row["message"], "links": links, "error": error}


def register_routes(router):
    router.post("/v1/grocery/add", add_item)
    router.post("/v1/grocery/list", list_items)
    router.post("/v1/grocery/remove", remove_item)
    router.post("/v1/grocery/clear", clear)
    router.post("/v1/grocery/sync", sync)          # internal (runbook); not an MCP tool
    router.post("/v1/grocery/learn", learn)
    router.post("/v1/grocery/cart", start_cart, 202)
    router.post("/v1/grocery/cart-status", cart_status)
