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
    if site == "weekly":
        r = weekly(fixtures=fixtures, store=store, out_dir=out_dir, defaults_path=defaults_path, log=log)
        return r["message"] + (f"\nדוח מלא: {r['report_path']}" if r.get("report_path") else "")
    get_adapter_class(site)
    lg(f"[{site}] start ({len(items)} items)")
    ad, picks, plan, mode = run_site(site, items, args, store, fixtures, d, lg)
    lg(f"[{site}] done: requests={plan['meta'].get('requests')} to_check={len(plan['to_check'])}")
    md_path, plan_path = write_outputs(out_dir, ad.name, report.render(picks, plan, mode, ad.display_name), plan)
    return report.compact(picks, plan, ad.display_name) + f"\nדוח מלא: {md_path}"


CART_SITES = ("rami_levy", "shufersal")
WEEKLY_SITES = ("super_hist", "rami_levy", "shufersal")
CHOICE_SITES = WEEKLY_SITES


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
         skipped: Optional[Dict[str, str]] = None, title: str = "רשימת קניות",
         choices: Optional[Dict[str, Dict[str, str]]] = None) -> Dict:
    """Read-only price search on both sites + cart LINKS (Ben opens them; nothing is written to any cart).
    sites = "both" | "rami_levy" | "shufersal" | list. fixtures = fixtures ROOT (<root>/<site>/index.json).
    skipped = {site: reason} for sites the caller decided not to search (circuit breaker) — shown in the message.
    Returns {message, links:{site:url|None}, urls:{site:[..]}, lines:{site:[plan lines]}, site_errors:{site:msg},
             blocked:{site:msg} (subset of site_errors: the site refused us -> caller's circuit breaker),
             missing:{site:[items not in link]}, plan_paths:{site:path}, items:int,
             needs_choice:[{item, site, suggested, candidates:[{id,name,price,brand,category,kashrut}]}],
             unmatched_choices:[item names in `choices` that are not on the list]}.
    choices = {list item: {site: product_id}} — a ONE-RUN pick (Hermes chose among needs_choice candidates);
    never saved as a preference."""
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
    unmatched = apply_choices(items, choices)
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
            "plan_paths": paths, "items": len(items),
            "needs_choice": needs_choice_rows(results, run_sites, errors), "unmatched_choices": unmatched}


def _ckey(s: str) -> str:
    from .relevance import clean_text
    from .normalize import norm_text
    return norm_text(clean_text(str(s or "")))


def apply_choices(items, choices) -> List[str]:
    """Pin the chosen product ids for this run only. Returns choice keys that match no list item."""
    unmatched = []
    for key, per_site in (choices or {}).items():
        k = _ckey(key)
        hit = [it for it in items if k in (_ckey(it.name), _ckey(it.raw_text or ""))]
        if not hit or not isinstance(per_site, dict):
            unmatched.append(str(key))
            continue
        for it in hit:
            for site, pid in per_site.items():
                if site in CHOICE_SITES and str(pid or "").strip():
                    it.pinned[site] = str(pid).strip()
                    it.chosen_sites.add(site)
    return unmatched


def _cand(c) -> Dict:
    from .relevance import product_category
    p = c.p
    return {"id": p.id, "name": p.name, "price": p.price, "brand": p.brand or "",
            "category": product_category(p), "kashrut": p.kashrut}


def needs_choice_rows(results, sites, errors) -> List[Dict]:
    rows = []
    for s in sites:
        if s in errors:
            continue
        for pk in results.get(s) or []:
            if pk is not None and getattr(pk, "needs_choice", False):
                rows.append({"item": pk.item.name, "list_text": pk.item.raw_text or pk.item.name, "site": s,
                             "suggested": _cand(pk.chosen) if pk.chosen else None,
                             "candidates": [_cand(c) for c in pk.choices]})
    return rows


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


def _run_sites(sites, items, d, fixtures, store, lg, parallel=True):
    """{site: (adapter, picks, plan, mode) | Exception}, {site: seconds}. Sites run in parallel threads
    (different hosts; each keeps its own pacing)."""
    import threading
    import time
    from .cli import run_site_or_fail
    res, secs = {}, {}

    def one(s):
        args = SimpleNamespace(delay=1.5, size=10, crawl_delay=None, deep_size=None)
        fx = os.path.join(fixtures, s) if fixtures else None
        if s == "super_hist" and fx and not os.path.exists(os.path.join(fx, "index.json")):
            fx = None   # Super-Hist "live" = the transport Command Center injected; never network from here
        lg(f"[{s}] start ({len(items)} items)")
        t0 = time.time()
        try:
            res[s] = run_site_or_fail(s, items, args, store if s == "rami_levy" else None, fx, d, lg)
        except Exception as e:   # one site's crash must not kill the others
            lg(f"[{s}] crashed: {type(e).__name__}: {e}")
            res[s] = e
        secs[s] = round(time.time() - t0, 1)
    if parallel and len(sites) > 1:
        ts = [threading.Thread(target=one, args=(s,), daemon=True) for s in sites]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    else:
        for s in sites:
            one(s)
    return res, secs


BLOCK_KINDS = ("CloudflareBlocked", "RateLimited", "RobotsDisallowed")


def weekly(fixtures: Optional[str] = None, store: Optional[str] = None, out_dir: Optional[str] = None,
           defaults_path: str = DEFAULTS, log: Union[None, Callable[[str], None], List[str]] = None,
           parallel: bool = True, skipped: Optional[Dict[str, str]] = None, title: str = "רשימת קניות",
           choices: Optional[Dict[str, Dict[str, str]]] = None, mode: str = "price",
           super_hist_gate: Optional[Callable[[], Dict]] = None,
           cart_builder: Optional[Callable[[List[Dict]], Dict]] = None,
           history_path: Optional[str] = None, items_override=None) -> Dict:
    """THE weekly-basket entry point (a future Tuesday routine calls this as-is and sends `message`).
    Part 1 הסופר החברתי (everything available there), part 2 ONE other store (Rami Levy or Shufersal, whichever
    is cheaper for the remainder), plus the evidence section (Super-Hist lines priced at Rami/Shufersal too).
    mode "price" = read-only (part 1 is a list of items); mode "cart" = also propose ONE batched Super-Hist cart
    approval via cart_builder(lines) (nothing is written before Ben approves; never checkout/order).
    super_hist_gate() -> {"status": logged_in|login_failed|login_required|site_blocked|site_error, "text",
    "requests"?} runs BEFORE any search. Command Center's gate logs in by itself when needed (ONE attempt per
    run, ≤3 min, code via the relay). login_failed / site_* → Super-Hist is skipped and the WHOLE list goes to
    the cheaper of Rami Levy / Shufersal (one line says why). login_required (a check-only gate) stops the run
    with no searches. A session lost MID-run ends Super-Hist only (no second login); the rest still runs.
    items_override = (items, defaults) for the CLI (--text / --list); default = the working list.
    Returns {status: done|login_required|empty, message, report_path, links, urls, lines, site_errors, blocked,
             needs_choice, unmatched_choices, items, result, super_hist_cart, runtime, estimate, other_site}."""
    import time
    from . import links as L
    from . import weekly as W
    from .cli import DEFAULT_OUT, write_outputs
    from .sites import get_adapter_class
    t0 = time.time()
    lg = _sink(log)
    skipped = dict(skipped or {})
    items, d = items_override if items_override is not None else current_items(defaults_path)
    unmatched = apply_choices(items, choices)
    base = {"links": {}, "urls": {}, "lines": {}, "site_errors": {}, "blocked": {}, "needs_choice": [],
            "unmatched_choices": unmatched, "items": len(items), "result": None, "super_hist_cart": None,
            "runtime": None, "estimate": None, "other_site": None, "report_path": None, "plan_paths": {}}
    if not items:
        return dict(base, status="empty", message="הרשימה ריקה — אין מה לבנות")
    est = W.estimate(len(items), build_cart=mode == "cart")
    base["estimate"] = est
    gate_requests, sh_login = 0, None
    if W.SH not in skipped and super_hist_gate is not None:
        g = super_hist_gate() or {}
        gate_requests = int(g.get("requests") or 1)
        lg(f"[super_hist] gate: {g.get('status')}")
        if g.get("status") == "login_required":
            # Nothing else is searched: without Super-Hist the remainder is unknown, and a full Rami/Shufersal
            # pass now would be repeated right after the login (~32 requests, ~3 min of Shufersal for nothing).
            q = str(g.get("text") or "🔐 צריך להתחבר לסופר החברתי")
            msg = q + "\n(עוד לא חיפשתי באף אתר; אחרי ההתחברות אריץ את הסל מחדש)"
            return dict(base, status="login_required", message=msg, login=g.get("login"))
        if g.get("status") != "logged_in":
            skipped[W.SH] = str(g.get("text") or g.get("status"))[:160]
        sh_login = g.get("login")
    run_sites = [s for s in WEEKLY_SITES if s not in skipped]
    if not run_sites:
        return dict(base, status="done", site_errors={s: f"דילגתי: {w}" for s, w in skipped.items()},
                    message="לא חיפשתי באף אתר: " + " · ".join(f"{W.NAMES.get(s, s)} — {w}"
                                                               for s, w in skipped.items()))
    out_dir = out_dir or DEFAULT_OUT
    raw, secs = _run_sites(run_sites, items, d, fixtures, store, lg, parallel)
    results, errors, blocked, lines, paths, requests = {}, {}, {}, {}, {}, {}
    for s in WEEKLY_SITES:
        r = raw.get(s)
        if s in skipped:
            errors[s] = f"דילגתי: {skipped[s]}"
            continue
        if isinstance(r, Exception) or r is None:
            errors[s] = f"{type(r).__name__}: {r}" if r is not None else "no result"
            continue
        ad, picks, plan, pmode = r
        results[s] = picks
        requests[s] = plan["meta"].get("requests")
        err = plan["meta"].get("site_error")
        if err:
            kind = plan["meta"].get("site_error_kind") or ""
            errors[s] = ("החיבור פג באמצע הריצה — צריך להתחבר מחדש" if s == W.SH and kind == "SessionExpired"
                         else err)
            if s != W.SH or kind in BLOCK_KINDS:
                blocked[s] = err
        _md, paths[s] = write_outputs(out_dir, ad.name, report.render(picks, plan, pmode, ad.display_name), plan)
        lines[s] = plan["lines"]
    if W.SH in skipped:
        requests[W.SH] = gate_requests
    elif W.SH in requests and requests[W.SH] is not None:
        requests[W.SH] += gate_requests
    sh_login_lost = W.SH in raw and not isinstance(raw[W.SH], Exception) and raw[W.SH] is not None and \
        raw[W.SH][2]["meta"].get("site_error_kind") == "SessionExpired"
    res = W.compute({s: results[s] for s in results}, errors, d.delivery)
    o = res.get("other")
    urls = {}
    if o and o["lines"]:
        want = {r["i"] for r in o["lines"]}
        urls[o["site"]], _missing = L.site_urls(o["site"], [l for k, l in enumerate(lines[o["site"]]) if k in want],
                                               title)
    sh_cart = None
    if mode == "cart" and res["super_hist"]["lines"]:
        cand = [{"product_id": r["product_id"], "quantity": r["qty"], "name": r["name"], "price": r["price"],
                 "weighted": r["by_weight"], "item": r["item"]} for r in res["super_hist"]["lines"]]
        if cart_builder is None:
            sh_cart = {"status": "unavailable", "text": "בניית עגלה זמינה רק דרך Command Center"}
        else:
            try:
                sh_cart = cart_builder(cand) or {}
            except Exception as e:   # the price part is still worth sending
                sh_cart = {"status": "site_error", "text": f"{type(e).__name__}: {str(e)[:120]}"}
            if sh_cart.get("status") not in ("pending_approval", "no_changes"):
                lg(f"[super_hist] cart proposal: {sh_cart.get('status')}: {sh_cart.get('text')}")
            requests[W.SH] = (requests.get(W.SH) or 0) + (1 if sh_cart.get("status") in ("pending_approval",
                                                                                          "no_changes") else 0)
    elapsed = round(time.time() - t0, 1)
    runtime = {"seconds": elapsed, "requests": requests, "per_site_seconds": secs}
    hist_txt = None
    if history_path:
        try:
            W.history_append(history_path, W.history_row(res, elapsed=elapsed, requests=requests, mode=mode))
            rows = W.history_read(history_path)
            hist_txt = W.history_summary(rows) if len(rows) >= 2 else None
        except OSError as e:
            lg(f"[weekly] history not written: {e}")
    gen = _il_now().strftime("%d.%m %H:%M")
    msg = report.weekly_message(res, urls.get(o["site"]) if o else [], gen, sh_cart, runtime, hist_txt, mode,
                                login_question=sh_login_lost, login=sh_login)
    full = report.weekly_full(res, urls.get(o["site"]) if o else [], gen, est, runtime, hist_txt, mode, sh_cart)
    os.makedirs(out_dir, exist_ok=True)
    rpath = os.path.join(out_dir, f"weekly-{dt.datetime.now():%Y%m%d-%H%M%S}.md")
    with open(rpath, "w", encoding="utf-8") as f:
        f.write(full)
    states = res["states"]
    rem = {r["i"] for r in res["remainder"]}
    need = []
    for s in results:
        if s in errors:
            continue
        for k, pk in enumerate(results[s]):
            if not getattr(pk, "needs_choice", False):
                continue
            if (s == W.SH and states[k] == "choice") or (o and s == o["site"] and k in rem):
                need.append({"item": pk.item.name, "list_text": pk.item.raw_text or pk.item.name, "site": s,
                             "suggested": _cand(pk.chosen) if pk.chosen else None,
                             "candidates": [_cand(c) for c in pk.choices]})
    sh_link = report.SH_CART_URL if (sh_cart or {}).get("status") in ("pending_approval", "no_changes") else None
    links = {W.SH: sh_link}
    if o:
        links[o["site"]] = (urls.get(o["site"]) or [None])[0]
    return dict(base, status="done", message=msg, report_path=rpath, links=links, urls=urls, lines=lines,
                site_errors=errors, blocked=blocked, needs_choice=need, result=res, super_hist_cart=sh_cart,
                runtime=runtime, other_site=o["site"] if o else None, plan_paths=paths,
                super_hist_login=sh_login)
