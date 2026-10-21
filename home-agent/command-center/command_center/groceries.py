"""Grocery list (Google Tasks "קניות"), learned preferences and the "תכין עגלה" cart job.

The working list lives in Google Tasks (see grocery_list.py); state/current_list.json is only its cache.
Default job ("weekly"): הסופר החברתי FIRST, the remainder at ONE other store (Rami Levy or Shufersal, whichever
is cheaper for it), every line priced on all three sites for the evidence section. All searches are READ-ONLY
and run in parallel (Super-Hist >= 10 s, Rami 1.5 s, Shufersal >= 10 s between requests). Super-Hist goes
through command_center.super_hist (its login session, URL policy, 24 h breaker). Logged out → the job logs in
by itself ONCE (super_hist.grocery_gate: start_login, code via the #OTP relay, ≤3 min); if that fails or times
out, Super-Hist is skipped and the whole list goes to Rami/Shufersal. Optional build mode proposes ONE batched Super-Hist
cart approval; nothing is written before Ben approves, and nothing here ever orders or pays. "both" / one site
= the older Rami+Shufersal links flow.
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
WEEKLY_SITES = ("super_hist", "rami_levy", "shufersal")
SITE_NAMES = {"rami_levy": "רמי לוי", "shufersal": "שופרסל", "super_hist": "הסופר החברתי"}
STALE_SECONDS = 30 * 60
ETA_SECONDS = 180            # "both": Shufersal ~13 searches x >=10 s
HISTORY_FILE = "weekly-history.jsonl"
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
    from . import super_hist
    super_hist.on_batch(_batch_done)      # an approval decided after a restart still lands on its run


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
    if out["text"].startswith(("נלמד", "נשמר כהערה")):
        grocery_db.log_learned(connection, item, change, p.get("reason", ""))
    return out


def preferences(p, _q):
    """List / update / delete durable preferences (defaults.user.yaml)."""
    action = str(p.get("action") or "list")
    idx = p.get("index")
    change = p.get("change")
    undo = bool(p.get("undo", False))
    full = bool(p.get("full", False))
    out = _text(_api().preferences, action, index=idx, change=change, undo=undo, full=full,
                defaults_path=str(DEFAULTS))
    # Audit every mutating call
    if action in ("update", "delete") or (action == "delete" and undo):
        grocery_db.log_learned(connection, f"pref:{action}",
                               {"index": idx, "change": change, "undo": undo, "full": full},
                               p.get("reason", ""))
    return out


def _skipped(sites, force=False):
    out = {}
    for s in sites:
        b = grocery_db.blocked(connection, s)
        if b:
            until = time.strftime("%d.%m %H:%M", time.localtime(b[0]))
            out[s] = f"נחסם לאחרונה, לא שולחת בקשות עד {until}"
    return out


def _release(held):
    if held[0]:
        held[0] = False
        _run_lock.release()


def _sh_hooks(ident):
    """(gate, cart_builder) for the weekly job. Offline fixture runs never touch the site (tests patch this)."""
    if FIXTURES:
        return None, None
    from . import super_hist
    return super_hist.grocery_gate, (lambda lines: super_hist.propose_batch(lines, run_id=ident))


def _wire_super_hist():
    from grocery.sites import super_hist as adapter
    from . import super_hist
    adapter.set_transport(None if FIXTURES else super_hist.grocery_transport)
    super_hist.on_batch(_batch_done)


def _batch_done(run_id, result):
    """Background Super-Hist batch write finished (or started): keep the latest state on the grocery run."""
    if not run_id:
        return
    grocery_db.update_extra(connection, run_id, {"super_hist_cart_result": result})
    log(f"cart {run_id} super_hist batch {result.get('status')}: written={len(result.get('written') or [])} "
        f"failed={len(result.get('failed') or [])} not_sent={len(result.get('not_sent') or [])}")


def _weekly_job(ident, skipped, warning=None, choices=None, build=False):
    held = [True]

    def finish(*args):
        _release(held)   # free the lock BEFORE the run reads as done: a caller may start the next job at once
        grocery_db.finish_run(connection, ident, *args)

    try:
        _wire_super_hist()
        gate, builder = _sh_hooks(ident)
        r = _api().weekly(fixtures=FIXTURES, out_dir=str(REPORTS_DIR), defaults_path=str(DEFAULTS),
                          log=lambda m: log(f"cart {ident} {m}"), skipped=skipped, choices=choices,
                          mode="cart" if build else "price", super_hist_gate=gate, cart_builder=builder,
                          history_path=None if FIXTURES else str(DATA_DIR / HISTORY_FILE))
        for s, err in r.get("blocked", {}).items():
            grocery_db.set_blocked(connection, s, err)
            log(f"cart {ident} {s} BLOCKED for 24h: {err}")
        for s in r.get("plan_paths", {}):
            if s not in r.get("site_errors", {}):
                grocery_db.set_ok(connection, s)
        message = r["message"]
        if warning:
            message = f"{warning}\n{message}"
        extra = {k: r[k] for k in ("needs_choice", "unmatched_choices") if r.get(k)}
        extra["mode"] = "weekly"
        if r["status"] == "login_required":
            extra["login_required"] = True
        if r.get("super_hist_login"):
            extra["super_hist_login"] = r["super_hist_login"]        # none | auto | failed
        if r.get("super_hist_cart"):
            c = r["super_hist_cart"]
            extra["super_hist_cart"] = {k: c.get(k) for k in ("status", "approval_id", "headline", "text",
                                                              "eta_seconds", "cart_url", "expected_total")
                                        if c.get(k) is not None}
        if r.get("other_site"):
            extra["other_site"] = r["other_site"]
        if r.get("report_path"):
            extra["report_path"] = r["report_path"]
        if r.get("runtime"):
            extra["runtime"] = r["runtime"]
        finish("done", message, r["links"], None, r.get("lines"), extra)
        log(f"cart {ident} weekly {r['status']} items={r['items']} other={r.get('other_site')} "
            f"sh_cart={(r.get('super_hist_cart') or {}).get('status')} runtime={(r.get('runtime') or {}).get('seconds')}")
    except Exception as exc:  # recorded AND logged; never swallowed
        error = f"{type(exc).__name__}: {exc}"
        log(f"cart {ident} failed: {error}\n{traceback.format_exc()}")
        finish("failed", None, None, error)
    finally:
        _release(held)


def _cart_job(ident, sites, skipped, warning=None, choices=None):
    held = [True]

    def finish(*args):
        _release(held)   # free the lock BEFORE the run reads as done: a caller may start the next job at once
        grocery_db.finish_run(connection, ident, *args)

    try:
        r = _api().cart(sites, fixtures=FIXTURES, out_dir=str(REPORTS_DIR), defaults_path=str(DEFAULTS),
                        log=lambda m: log(f"cart {ident} {m}"), skipped=skipped, choices=choices)
        for s, err in r.get("blocked", {}).items():
            grocery_db.set_blocked(connection, s, err)
            log(f"cart {ident} {s} BLOCKED for 24h: {err}")
        for s in r.get("plan_paths", {}):
            if s not in r.get("site_errors", {}):
                grocery_db.set_ok(connection, s)
        message = r["message"]
        if warning:
            message = f"{warning}\n{message}"
        extra = {k: r[k] for k in ("needs_choice", "unmatched_choices") if r.get(k)}
        finish("done", message, r["links"], None, r.get("lines"), extra)
        log(f"cart {ident} done items={r['items']} links={ {s: bool(u) for s, u in r['links'].items()} } "
            f"needs_choice={len(r.get('needs_choice') or [])} choices={len(choices or {})}")
    except Exception as exc:  # recorded AND logged; never swallowed (price_watches lesson)
        error = f"{type(exc).__name__}: {exc}"
        log(f"cart {ident} failed: {error}\n{traceback.format_exc()}")
        finish("failed", None, None, error)
    finally:
        _release(held)


def _choices(raw):
    """{list item: {site: product_id}} — one-run picks among needs_choice candidates (never saved)."""
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
        raise ValueError('choices must be {"<list item>": {"rami_levy": "<id>", "shufersal": "<id>"}}')
    bad = [s for v in raw.values() for s in v if s not in WEEKLY_SITES]
    if bad:
        raise ValueError(f"choices: unknown site(s) {bad}; use super_hist / rami_levy / shufersal")
    return {str(k): {s: str(i) for s, i in v.items() if str(i or "").strip()} for k, v in raw.items()}


def start_cart(p, _q):
    sites = p.get("sites", "weekly") or "weekly"
    weekly = sites == "weekly"
    want = list(WEEKLY_SITES) if weekly else list(CART_SITES) if sites == "both" else [sites]
    if not weekly and any(s not in CART_SITES for s in want):
        raise ValueError("sites must be weekly, both, rami_levy or shufersal")
    build = bool(p.get("build_super_hist_cart", False))
    if build and not weekly:
        raise ValueError("build_super_hist_cart needs sites=weekly")
    choices = _choices(p.get("choices"))
    if choices and not weekly and any("super_hist" in v for v in choices.values()):
        raise ValueError("choices for super_hist need sites=weekly")
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
    if weekly:
        from grocery import weekly as W
        threading.Thread(target=_weekly_job, args=(ident, skipped, warning, choices, build), daemon=True).start()
        eta = W.estimate(len(st["items"]), build_cart=build)["wall_seconds"] + 30
    else:
        threading.Thread(target=_cart_job, args=(ident, want, skipped, warning, choices), daemon=True).start()
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
    out = {"run_id": ident, "status": status, "seconds": (row["finished"] or int(time.time())) - row["started"],
           "message": row["message"], "links": links, "error": error}
    if row.get("extra_json"):
        out.update(json.loads(row["extra_json"]))
    return out


def register_routes(router):
    router.post("/v1/grocery/add", add_item)
    router.post("/v1/grocery/list", list_items)
    router.post("/v1/grocery/remove", remove_item)
    router.post("/v1/grocery/clear", clear)
    router.post("/v1/grocery/sync", sync)          # internal (runbook); not an MCP tool
    router.post("/v1/grocery/learn", learn)
    router.post("/v1/grocery/preferences", preferences)
    router.post("/v1/grocery/cart", start_cart, 202)
    router.post("/v1/grocery/cart-status", cart_status)
