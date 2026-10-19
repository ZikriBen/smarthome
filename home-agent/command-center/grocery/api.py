"""Thin Python API for Hermes: a persistent working list + learn + run. Every call returns a short string.

State: <root>/state/current_list.json (override with $GROCERY_STATE_DIR). Defaults never go to Hermes;
only compact one-liners come back.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Union

from . import freetext, report
from .defaults import load_defaults
from .learn import learn_preference as _learn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULTS = os.path.join(ROOT, "defaults.yaml")


def state_path() -> str:
    d = os.environ.get("GROCERY_STATE_DIR") or os.path.join(ROOT, "state")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "current_list.json")


def _load() -> Dict:
    p = state_path()
    if not os.path.exists(p):
        return {"items": [], "updated_at": None}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _save(st: Dict) -> None:
    st["updated_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    p = state_path()
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(p + ".tmp", p)


def _qty_txt(e) -> str:
    if e.get("qty") is None:
        return ""
    q = f"{e['qty']:g}"
    return f" ×{q} ק\"ג" if e.get("qty_unit") == "kg" else f" ×{q}"


def add_item(text: str, defaults_path: str = DEFAULTS) -> str:
    """Add one or more items from free text ("חלב, 2 מלפפון"). Same item again -> quantity replaced if given."""
    d = load_defaults(defaults_path)
    st = _load()
    added = []
    for pl in freetext.parse_text(text, split=d.split_known):
        it = d.resolve_parsed(pl)
        e = {"text": pl.text, "item": it.name, "qty": pl.qty, "qty_unit": pl.qty_unit, "known": it.source == "defaults"}
        ex = next((x for x in st["items"] if x["item"] == it.name), None)
        if ex:
            if pl.qty is not None:
                ex.update(qty=pl.qty, qty_unit=pl.qty_unit)
            added.append(f"{it.name}{_qty_txt(ex)} (עודכן)")
        else:
            st["items"].append(e)
            added.append(f"{it.name}{_qty_txt(e)}" + ("" if e["known"] else " (חדש — חיפוש חופשי)"))
    if not added:
        return "לא זוהו פריטים"
    _save(st)
    return "נוסף: " + " · ".join(added) + f" (ברשימה: {len(st['items'])})"


def list_items() -> str:
    st = _load()
    if not st["items"]:
        return "הרשימה ריקה"
    return f"{len(st['items'])} פריטים: " + " · ".join(f"{e['item']}{_qty_txt(e)}" for e in st["items"])


def remove_item(item: Union[str, int], defaults_path: str = DEFAULTS) -> str:
    st = _load()
    idx = None
    if isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit()):
        i = int(item) - 1
        idx = i if 0 <= i < len(st["items"]) else None
    else:
        name = load_defaults(defaults_path).lookup(item) or item.strip()
        idx = next((i for i, e in enumerate(st["items"]) if e["item"] == name or e["text"] == item.strip()), None)
        if idx is None:   # unique partial match ("קמח" -> "קמח מלא")
            hits = [i for i, e in enumerate(st["items"]) if item.strip() in e["item"]]
            idx = hits[0] if len(hits) == 1 else None
    if idx is None:
        return f"לא נמצא ברשימה: {item}"
    e = st["items"].pop(idx)
    _save(st)
    return f"הוסר: {e['item']} (ברשימה: {len(st['items'])})"


def clear() -> str:
    st = _load()
    n = len(st["items"])
    st["items"] = []
    _save(st)
    return f"הרשימה נוקתה ({n} פריטים)"


def learn_preference(item: str, change, reason: str = "", site: Optional[str] = None,
                     defaults_path: str = DEFAULTS) -> str:
    return _learn(item, change, reason=reason, defaults_path=defaults_path, site=site)


def current_items(defaults_path: str = DEFAULTS):
    d = load_defaults(defaults_path)
    out = []
    for e in _load()["items"]:
        it = d.resolve(e["item"] if e.get("known") else e["text"])
        it.raw_text = e["text"]
        if e.get("qty") is not None:
            it.qty, it.qty_unit = e["qty"], e.get("qty_unit")
        if not e.get("known"):
            it.source = "text"
        out.append(it)
    return out, d


def _sink(log) -> Callable[[str], None]:
    """log=None -> quiet (old behavior); a callable -> called per line; a list -> lines appended."""
    if log is None:
        return lambda m: None
    if isinstance(log, list):
        return log.append
    if callable(log):
        return log
    raise TypeError("log must be None, a callable or a list")


def run(site: str = "rami_levy", fixtures: Optional[str] = None, store: Optional[str] = None,
        out_dir: Optional[str] = None, defaults_path: str = DEFAULTS,
        log: Union[None, Callable[[str], None], List[str]] = None) -> str:
    """Phase 1 (read-only) on the working list. site = rami_levy | shufersal | compare.
    fixtures = a fixtures dir (single site) or fixtures root (compare). Never touches the cart.
    log: per-request / per-item progress lines (searches, retries, failures) — None keeps it quiet.
    Items whose search failed or found nothing are listed as 'לבדיקה' and are never silently left out of
    the total: the summary says how many items the total excludes. In compare, a site that is blocked
    marks all its items 'לבדיקה' for that site instead of failing the whole run."""
    from .cli import DEFAULT_OUT, run_site, run_site_or_fail, write_outputs
    from .sites import get_adapter_class
    lg = _sink(log)
    items, d = current_items(defaults_path)
    if not items:
        return "הרשימה ריקה — אין מה להריץ"
    args = SimpleNamespace(delay=1.5, size=10, crawl_delay=None, deep_size=None)
    out_dir = out_dir or DEFAULT_OUT
    if site == "compare":
        results, names, plans = {}, {}, {}
        for s in ("rami_levy", "shufersal"):
            fx = os.path.join(fixtures, s) if fixtures else None
            lg(f"[{s}] start ({len(items)} items)")
            ad, picks, plan, mode = run_site_or_fail(s, items, args, store if s == "rami_levy" else None, fx, d, lg)
            write_outputs(out_dir, ad.name, report.render(picks, plan, mode, ad.display_name), plan)
            results[ad.name], names[ad.name], plans[ad.name] = picks, ad.display_name, plan
            lg(f"[{s}] done: requests={plan['meta'].get('requests')} to_check={len(plan['to_check'])}")
        md = report.compare(results, names)
        path = os.path.join(out_dir, f"compare-{dt.datetime.now():%Y%m%d-%H%M%S}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        return report.compare_compact(results, names) + f"\nדוח מלא: {path}"
    get_adapter_class(site)
    lg(f"[{site}] start ({len(items)} items)")
    ad, picks, plan, mode = run_site(site, items, args, store, fixtures, d, lg)
    lg(f"[{site}] done: requests={plan['meta'].get('requests')} to_check={len(plan['to_check'])}")
    md_path, plan_path = write_outputs(out_dir, ad.name, report.render(picks, plan, mode, ad.display_name), plan)
    return report.compact(picks, plan, ad.display_name) + f"\nדוח מלא: {md_path}"


CART_SITES = ("rami_levy", "shufersal")


def _il_now():
    """Israel local time for the message label (containers run in UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Asia/Jerusalem"))
    except Exception:
        return dt.datetime.now().astimezone()


def cart(sites: Union[str, List[str]] = "both", fixtures: Optional[str] = None, store: Optional[str] = None,
         out_dir: Optional[str] = None, defaults_path: str = DEFAULTS,
         log: Union[None, Callable[[str], None], List[str]] = None, parallel: bool = True,
         skipped: Optional[Dict[str, str]] = None, title: str = "רשימת קניות") -> Dict:
    """Read-only price search on both sites + cart LINKS (Ben opens them; nothing is written to any cart).
    sites = "both" | "rami_levy" | "shufersal" | list. fixtures = fixtures ROOT (<root>/<site>/index.json).
    skipped = {site: reason} for sites the caller decided not to search (circuit breaker) — shown in the message.
    Returns {message, links:{site:url|None}, urls:{site:[..]}, lines:{site:[plan lines]}, site_errors:{site:msg},
             blocked:{site:msg} (subset of site_errors: the site refused us -> caller's circuit breaker),
             missing:{site:[items not in link]}, plan_paths:{site:path}, items:int}."""
    import threading
    from . import links as L
    from .cli import DEFAULT_OUT, run_site_or_fail, write_outputs
    lg = _sink(log)
    skipped = dict(skipped or {})
    want = list(CART_SITES) if sites in (None, "both") else ([sites] if isinstance(sites, str) else list(sites))
    for s in want:
        if s not in CART_SITES:
            raise ValueError(f"unknown site {s}")
    run_sites = [s for s in want if s not in skipped]
    items, d = current_items(defaults_path)
    empty = {"message": "הרשימה ריקה — אין מה לבנות", "links": {}, "urls": {}, "lines": {}, "site_errors": {},
             "blocked": {},
             "missing": {}, "plan_paths": {}, "items": 0}
    if not items:
        return empty
    out_dir = out_dir or DEFAULT_OUT
    res: Dict[str, tuple] = {}

    def one(s):
        args = SimpleNamespace(delay=1.5, size=10, crawl_delay=None, deep_size=None)
        fx = os.path.join(fixtures, s) if fixtures else None
        lg(f"[{s}] start ({len(items)} items)")
        try:
            res[s] = run_site_or_fail(s, items, args, store if s == "rami_levy" else None, fx, d, lg)
        except Exception as e:   # unexpected crash of one site must not kill the other
            lg(f"[{s}] crashed: {type(e).__name__}: {e}")
            res[s] = e
    if parallel and len(run_sites) > 1:
        ts = [threading.Thread(target=one, args=(s,), daemon=True) for s in run_sites]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    else:
        for s in run_sites:
            one(s)
    results, names, urls, lines, errors, missing, paths, blocked = {}, {}, {}, {}, {}, {}, {}, {}
    for s in run_sites:
        r = res.get(s)
        if isinstance(r, Exception) or r is None:
            errors[s] = f"{type(r).__name__}: {r}" if r is not None else "no result"
            from .sites import get_adapter_class
            names[s] = get_adapter_class(s).display_name
            results[s] = [None] * len(items)
            continue
        ad, picks, plan, mode = r
        names[s], results[s] = ad.display_name, picks
        if plan["meta"].get("site_error"):   # blocked / robots / session: stopped, no more requests
            errors[s] = blocked[s] = plan["meta"]["site_error"]
        _md, paths[s] = write_outputs(out_dir, ad.name, report.render(picks, plan, mode, ad.display_name), plan)
        lines[s] = plan["lines"]
        if s not in errors:
            urls[s], missing[s] = L.site_urls(s, plan["lines"], title)
        lg(f"[{s}] done: requests={plan['meta'].get('requests')} to_check={len(plan['to_check'])} "
           f"links={len(urls.get(s) or [])} not_linked={len(missing.get(s) or [])}")
    for s in skipped:
        from .sites import get_adapter_class
        names[s] = get_adapter_class(s).display_name
    ok = {s: results[s] for s in run_sites}
    if not ok:
        msg = "לא חיפשתי באף אתר: " + " · ".join(f"{names[s]} — {w}" for s, w in skipped.items())
    else:
        msg = report.cart_message(ok, names, urls, errors,
                                  {s: skipped[s] for s in skipped},
                                  _il_now().strftime("%H:%M"),
                                  delivery=d.delivery)
    return {"message": msg, "links": {s: (urls.get(s) or [None])[0] for s in want},
            "urls": urls, "lines": lines, "site_errors": errors, "blocked": blocked, "missing": missing,
            "plan_paths": paths, "items": len(items)}


def preferences(action: str = "list", index: int = None, change=None, undo: bool = False,
                full: bool = False, defaults_path: str = DEFAULTS) -> str:
    """List / update / delete durable preferences. action = list | update | delete.
    full=True (or action=full) includes built-in defaults in the numbered list."""
    from . import prefs as P
    act = (action or "list").strip().lower()
    if act in ("list", "full"):
        return P.list_text(defaults_path, full=full or act == "full")
    if act == "delete":
        if undo:
            return P.delete(0, defaults_path, undo=True)
        if index is None:
            return "צריך מספר העדפה למחיקה (מהרשימה של grocery_preferences)"
        return P.delete(int(index), defaults_path, undo=False, full=bool(full))
    if act == "update":
        if index is None or change is None:
            return "צריך מספר העדפה ואת השינוי (למשל change=\"פרווה\")"
        return P.update(int(index), change, defaults_path, full=bool(full))
    return f"action לא מוכר: {action} (list|update|delete)"
