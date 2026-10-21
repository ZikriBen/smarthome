"""CLI. Run `python3 grocery.py -h`. Phase 2 (--apply-cart) is disabled unless explicitly requested AND confirmed."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import List, Optional

from . import freetext, report
from . import normalize as N
from .cartlogic import MERGE_POLICIES, CartError, format_result
from .defaults import load_defaults, upsert_default
from .http import Client, CloudflareBlocked, FixtureSearcher, HttpError, RobotsDisallowed, SessionExpired
from .listfile import ListError, load_list
from .model import Item
from .picker import Pick, Picker, build_plan, pack_search_plan
from .sites import get_adapter_class

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEFAULTS = os.path.join(HERE, "defaults.yaml")
DEFAULT_OUT = os.path.join(HERE, "out")
DEFAULT_FIXTURES_ROOT = os.path.join(HERE, "fixtures")


def _log_fn(verbose):
    return (lambda m: print(m, file=sys.stderr)) if verbose else (lambda m: None)


def load_items(args, defaults) -> (List[Item], Optional[str]):
    store = None
    if args.list:
        lf = load_list(args.list, defaults)
        return lf.items, lf.store
    text = args.text
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            text = f.read()
    if not text:
        from . import api
        items, _ = api.current_items(args.defaults)
        if not items:
            raise ListError("give --list FILE, --text '...', --text-file FILE, or add items first (grocery.py add ...)")
        return items, store
    return [defaults.resolve_parsed(pl) for pl in freetext.parse_text(text)], store


def make_adapter(site: str, args, store: Optional[str], fixtures_dir: Optional[str], log):
    cls = get_adapter_class(site)
    if fixtures_dir and store is None:     # offline: reproduce the store the fixtures were captured in
        try:
            with open(os.path.join(fixtures_dir, "index.json"), encoding="utf-8") as f:
                store = json.load(f).get("_store") or None
        except (OSError, ValueError):
            store = None
    adapter = cls(store=store)
    if fixtures_dir:
        adapter.searcher = FixtureSearcher(fixtures_dir, log, results_key=cls.fixture_results_key)
        mode = f"fixtures ({fixtures_dir})"
    else:
        client = Client(delay=args.delay, log=log, block_msg=cls.block_msg,
                        block_detector=getattr(sys.modules[cls.__module__], "is_block_page", None),
                        crawl_delay=site_crawl_delay(cls, args, log), robots=cls.robots)
        adapter.searcher = adapter.make_live_searcher(client, size=args.size)
        mode = "live"
    return adapter, mode


def site_crawl_delay(cls, args, log) -> float:
    """robots.txt Crawl-delay is a floor (Shufersal: 10 s). --crawl-delay can raise it, never lower it."""
    want = getattr(args, "crawl_delay", None)
    floor = float(cls.crawl_delay or 0)
    if want is None:
        return floor
    if want < floor:
        log(f"[{cls.name}] --crawl-delay {want:g} is below robots.txt Crawl-delay {floor:g}; using {floor:g}")
        return floor
    return float(want)


def _robots_note(cls, mode) -> Optional[str]:
    if mode != "live" or cls.robots is None:
        return None
    inside = cls.robots.in_visit_time(dt.datetime.now(dt.timezone.utc))
    if inside is False:
        return f"⚠️ מחוץ לחלון Visit-time של robots.txt ({cls.robots.visit_time} UTC)"
    return None


def run_site(site, items, args, store, fixtures_dir, defaults, log):
    adapter, mode = make_adapter(site, args, store, fixtures_dir, log)
    picker = Picker(adapter, log=log, produce_defaults=defaults.produce,
                    deep_size=getattr(args, "deep_size", None),
                    max_extra_searches=getattr(adapter, "max_extra_searches", 2))
    picks = [picker.pick(it) for it in items]
    sr = adapter.searcher
    reqs = sr.client.request_count if hasattr(sr, "client") else getattr(sr, "request_count", None)
    meta = {"generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "mode": mode,
            "searches": picker.search_count, "requests": reqs, "tool": "grocery-poc 0.3",
            "crawl_delay": sr.client.delay if hasattr(sr, "client") else None,
            "robots_note": _robots_note(type(adapter), mode),
            "search_errors": dict(picker.errors)}
    if meta["robots_note"]:
        log(f"[{adapter.name}] {meta['robots_note']}")
    plan = build_plan(picks, adapter.name, adapter.store, meta)
    return adapter, picks, plan, mode


def run_site_or_fail(site, items, args, store, fixtures_dir, defaults, log):
    """For compare: a site-level failure (blocked / robots / session) marks EVERY item of that site as
    'לבדיקה' instead of aborting the other site."""
    try:
        return run_site(site, items, args, store, fixtures_dir, defaults, log)
    except (CloudflareBlocked, SessionExpired, RobotsDisallowed) as e:
        cls = get_adapter_class(site)
        adapter = cls(store=store)
        msg = str(e)[:120]
        log(f"[{adapter.name}] site FAILED: {msg}")
        picks = [Pick(it, None, [], None, "none", [f"שגיאת אחזור — לבדיקה ({msg})"], list(it.terms), {},
                      adapter.name, error=msg) for it in items]
        mode = "live (failed)" if not fixtures_dir else f"fixtures ({fixtures_dir})"
        meta = {"generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "mode": mode,
                "searches": 0, "requests": None, "tool": "grocery-poc 0.3", "site_error": msg,
                "site_error_kind": type(e).__name__}
        return adapter, picks, build_plan(picks, adapter.name, adapter.store, meta), mode


def write_outputs(out_dir, site, md, plan):
    os.makedirs(out_dir, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = os.path.join(out_dir, f"{site}-report-{ts}.md")
    plan_path = os.path.join(out_dir, f"{site}-plan-{ts}.json")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    for p in (plan_path, os.path.join(out_dir, f"{site}-plan-latest.json")):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
    return md_path, plan_path


def confirm(args, plan, adapter) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("refusing: --apply-cart needs --yes or an interactive confirmation", file=sys.stderr)
        return False
    print(f"\nAbout to MERGE {len(plan['cart'])} products into your {adapter.display_name} cart "
          f"(policy={args.merge_policy}). No checkout will happen.")
    return input("Type 'yes' to continue: ").strip().lower() == "yes"


def apply_cart(adapter, plan, args, log, after_phase1: bool = False) -> int:
    raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")


def cmd_run(args) -> int:
    log = _log_fn(args.verbose)
    defaults = load_defaults(args.defaults)
    if args.plan:
        if not args.apply_cart:
            print("--plan is only used together with --apply-cart", file=sys.stderr)
            return 2
        with open(args.plan, encoding="utf-8") as f:
            plan = json.load(f)
        adapter = get_adapter_class(plan.get("site") or args.site)(store=plan.get("store"))
        return apply_cart(adapter, plan, args, log)
    items, list_store = load_items(args, defaults)
    store = args.store or list_store or os.environ.get("GROCERY_STORE") or None
    adapter, picks, plan, mode = run_site(args.site, items, args, store, args.fixtures, defaults, log)
    md = report.render(picks, plan, mode, adapter.display_name)
    md_path, plan_path = write_outputs(args.out, adapter.name, md, plan)
    print(md if args.full else report.compact(picks, plan, adapter.display_name))
    print(f"\nreport: {md_path}\nplan:   {plan_path}")
    if args.apply_cart:
        return apply_cart(adapter, plan, args, log, after_phase1=mode == "live")
    return 0


def cmd_compare(args) -> int:
    log = _log_fn(args.verbose)
    defaults = load_defaults(args.defaults)
    items, list_store = load_items(args, defaults)
    results, names = {}, {}
    blocked = []
    for site in [s.strip() for s in args.sites.split(",") if s.strip()]:
        fx = os.path.join(args.fixtures_root, get_adapter_class(site).name) if args.fixtures_root else None
        store = (args.store or list_store) if get_adapter_class(site).name == "rami_levy" else None
        adapter, picks, plan, mode = run_site_or_fail(site, items, args, store, fx, defaults, log)
        if plan["meta"].get("site_error"):
            blocked.append(f"{adapter.display_name}: {plan['meta']['site_error']}")
        md = report.render(picks, plan, mode, adapter.display_name)
        write_outputs(args.out, adapter.name, md, plan)
        results[adapter.name], names[adapter.name] = picks, adapter.display_name
    md = report.compare(results, names)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"compare-{dt.datetime.now():%Y%m%d-%H%M%S}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    print(md + f"\ncompare report: {path}")
    if blocked:   # that site was stopped at its first block; the other site still ran
        print("ERROR: stopped (not retried): " + " | ".join(blocked), file=sys.stderr)
        return 10
    return 0


def cmd_weekly(args) -> int:
    """Weekly basket: Super-Hist first, the remainder at ONE other store, evidence section. Read-only.
    Super-Hist live search exists only inside Command Center (its login session); elsewhere it is skipped."""
    from . import api
    from . import weekly as W
    if args.estimate:
        e = W.estimate(args.estimate, build_cart=args.with_cart_estimate)
        print(json.dumps(e, ensure_ascii=False, indent=1))
        print(f"wall ≈ {W.fmt_seconds(e['wall_seconds'])} (sites in parallel), requests {e['requests_total']}")
        return 0
    log = _log_fn(args.verbose)
    defaults = load_defaults(args.defaults)
    have_text = args.list or args.text or args.text_file
    override = (load_items(args, defaults)[0], defaults) if have_text else None
    skipped = {}
    if not args.fixtures_root:
        try:
            from command_center import super_hist as SHC      # inside the Command Center container only
            from .sites import super_hist as SHA
            SHA.set_transport(SHC.grocery_transport)
        except Exception:
            skipped["super_hist"] = "חיפוש בסופר החברתי רק דרך Command Center (grocery_cart)"
    data_dir = os.environ.get("GROCERY_DATA_DIR") or os.path.join(HERE, "data")
    hist = args.history if args.history else (None if args.fixtures_root else
                                               os.path.join(data_dir, "weekly-history.jsonl"))
    r = api.weekly(fixtures=args.fixtures_root, store=args.store, out_dir=args.out, defaults_path=args.defaults,
                   log=log, skipped=skipped, history_path=hist, items_override=override)
    print(open(r["report_path"], encoding="utf-8").read() if args.full and r.get("report_path") else r["message"])
    if r.get("report_path"):
        print(f"\nweekly report: {r['report_path']}")
    if r["status"] == "login_required":
        return 11
    return 10 if r.get("blocked") else 0


def cmd_capture(args) -> int:
    """Live: save the raw search response for every term of the list as fixtures (run on the NUC)."""
    log = _log_fn(True)
    defaults = load_defaults(args.defaults)
    items, list_store = load_items(args, defaults)
    store = args.store or list_store
    cls = get_adapter_class(args.site)
    adapter, mode = make_adapter(args.site, args, store, None, log)
    deep = args.deep_size or adapter.deep_size
    sizes = {}
    for it in items:   # pack_preference items: deep page + the possible extra pack searches
        pp = bool(it.pack_preference) and not it.pinned_for(cls.name)
        for t in it.terms + it.fallback_terms + ([t for t, _ in pack_search_plan(it)] if pp else []):
            sizes[t] = max(sizes.get(t, 0), deep if pp else 0)
    terms = list(sizes)
    os.makedirs(args.dest, exist_ok=True)
    index = {"_captured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
             "_site": cls.name, "_store": adapter.store}
    for i, t in enumerate(terms, 1):
        rows = adapter.search(t, size=sizes[t] or None)
        fn = f"q{i:02d}.json"
        with open(os.path.join(args.dest, fn), "w", encoding="utf-8") as f:
            json.dump({"q": t, cls.fixture_results_key: rows}, f, ensure_ascii=False, indent=1)
        index[t] = fn
        print(f"{t}: {len(rows)} rows -> {fn}")
    with open(os.path.join(args.dest, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print(f"saved {len(terms)} searches to {args.dest} (use with --fixtures {args.dest})")
    return 0


def cmd_search(args) -> int:
    """READ-ONLY: one search, every normalized row (pack, price, unit price). No cart, no state."""
    log = _log_fn(args.verbose)
    cls = get_adapter_class(args.site)
    store = (args.store or os.environ.get("GROCERY_STORE")) if cls.name == "rami_levy" else None
    adapter, mode = make_adapter(args.site, args, store, args.fixtures, log)
    unit = args.unit
    shown = 0
    for term in args.terms:
        rows = adapter.search(term, size=args.size)
        print(f"== {adapter.display_name} q={term!r} size={args.size} -> {len(rows)} rows ({mode})")
        for raw in rows:
            p = adapter.normalize(raw, unit)
            if p is None:
                continue
            if args.grep and args.grep not in p.name:
                continue
            shown += 1
            pr = f"₪{p.price:.2f}" if p.price is not None else "?"
            print(f"  {p.id:>10}  {pr:>8}  {p.pack.label():<14} {N.unit_price_label(p.unit_price, p.pack.unit):<16}"
                  f" {'' if p.available else '[not in store] '}{p.name}")
    sr = adapter.searcher
    reqs = sr.client.request_count if hasattr(sr, "client") else getattr(sr, "request_count", None)
    print(f"requests: {reqs}  (read-only; no cart, no login)")
    return 0


def cmd_parse(args) -> int:
    defaults = load_defaults(args.defaults)
    text = args.text
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            text = f.read()
    for pl in freetext.parse_text(text or ""):
        it = defaults.resolve_parsed(pl)
        q = "default" if it.qty is None else f"{it.qty:g} {it.qty_unit or ''}".strip()
        print(f"«{pl.raw}» → {it.name} [{it.source}] qty={q} terms={it.terms}"
              + (f" brands={it.brands}" if it.brands else "") + (f" fallback={it.fallback_terms}" if it.fallback_terms else ""))
    return 0


def _csv(v):
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def cmd_remember(args) -> int:
    user = args.user_file or os.path.splitext(args.defaults)[0] + ".user.yaml"
    entry = upsert_default(args.alias, user, terms=_csv(args.terms), brands=_csv(args.brands),
                           aliases=_csv(args.aliases), keywords=_csv(args.keywords),
                           fallback_terms=_csv(args.fallback_terms), extra_terms=_csv(args.extra_terms),
                           exclude_words=_csv(args.exclude_words),
                           require_any=_csv(args.require_any),
                           prefer_words=_csv(args.prefer_words), qty=args.qty, unit=args.unit,
                           produce=True if args.produce else None, note=args.note,
                           pack_preference=[float(x) for x in _csv(args.pack_preference)] if args.pack_preference else None)
    print(f"saved {args.alias!r} -> {user}: {json.dumps(entry, ensure_ascii=False)}")
    return 0


def cmd_list_ops(args) -> int:
    from . import api
    if args.cmd == "add":
        print(api.add_item(" , ".join(args.text), args.defaults))
    elif args.cmd == "list":
        print(api.list_items())
    elif args.cmd == "remove":
        print(api.remove_item(args.item, args.defaults))
    elif args.cmd == "clear":
        print(api.clear())
    return 0


def cmd_learn(args) -> int:
    from .learn import LearnError, learn_preference
    change = json.loads(args.change) if args.change.strip().startswith("{") else args.change
    try:
        print(learn_preference(args.item, change, reason=args.reason or "", defaults_path=args.defaults,
                               user_path=args.user_file, site=args.site))
    except LearnError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="grocery.py", description="Grocery cart builder POC (Super-Hist, Rami Levy, Shufersal)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--list", help="list.yaml / list.json")
        p.add_argument("--text", help='free text, e.g. "חלב, 2 מלפפון, חצי קילו עגבניות"')
        p.add_argument("--text-file", help="file with free text (commas/newlines)")
        p.add_argument("--defaults", default=DEFAULT_DEFAULTS, help="defaults.yaml (+ defaults.user.yaml next to it)")
        p.add_argument("--store", help="Rami Levy store id (default: list file, $GROCERY_STORE, else 9001)")
        p.add_argument("--size", type=int, default=10, help="results per search (default 10)")
        p.add_argument("--delay", type=float, default=1.5, help="seconds between requests (default 1.5 + jitter)")
        p.add_argument("--crawl-delay", type=float, default=None,
                       help="min seconds between requests; default = the site's robots.txt Crawl-delay "
                            "(Shufersal 10, Rami Levy 0); can only be raised")
        p.add_argument("--deep-size", type=int, default=None,
                       help="results per search for pack_preference items (default 40)")
        p.add_argument("--out", default=DEFAULT_OUT, help="output dir (default ./out)")
        p.add_argument("-v", "--verbose", action="store_true")

    r = sub.add_parser("run", help="phase 1: build the cart plan for one site (optionally phase 2)")
    common(r)
    r.add_argument("--site", default="rami_levy", help="rami_levy | shufersal")
    r.add_argument("--fixtures", help="offline mode: dir with index.json + saved search responses")
    r.add_argument("--full", action="store_true", help="print the full Markdown report instead of the compact view")
    g = r.add_argument_group("phase 2 (DISABLED by default — writes to your real cart, never checks out)")
    g.add_argument("--apply-cart", action="store_true", help="merge the plan into the logged-in cart")
    g.add_argument("--yes", action="store_true", help="confirm --apply-cart without a prompt")
    g.add_argument("--session", help="session JSON (default /data/<site>-session.json)")
    g.add_argument("--merge-policy", choices=MERGE_POLICIES, default="max")
    g.add_argument("--plan", help="apply an existing plan JSON instead of re-running phase 1")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="run phase 1 on several sites and compare (read-only)")
    common(c)
    c.add_argument("--sites", default="rami_levy,shufersal")
    c.add_argument("--fixtures-root", help="offline: <root>/<site>/index.json")
    c.set_defaults(func=cmd_compare)

    w = sub.add_parser("weekly-basket", help="weekly basket: Super-Hist first + ONE cheaper other store (read-only)")
    common(w)
    w.add_argument("--fixtures-root", help="offline: <root>/<site>/index.json for super_hist, rami_levy, shufersal")
    w.add_argument("--history", help="append a JSON line per run (default data/weekly-history.jsonl when live)")
    w.add_argument("--full", action="store_true", help="print the Markdown report instead of the message")
    w.add_argument("--estimate", type=int, metavar="LINES", help="only print the runtime/request estimate")
    w.add_argument("--with-cart-estimate", action="store_true", help="--estimate incl. the cart-build approval")
    w.set_defaults(func=cmd_weekly)

    cp = sub.add_parser("capture", help="LIVE: save raw search responses for the list's terms as fixtures (NUC only)")
    common(cp)
    cp.add_argument("--site", default="rami_levy")
    cp.add_argument("--dest", required=True, help="e.g. fixtures/live/rami_levy")
    cp.set_defaults(func=cmd_capture)

    se = sub.add_parser("search", help="READ-ONLY: print all normalized results for search term(s)")
    common(se)
    se.add_argument("terms", nargs="+")
    se.add_argument("--site", default="rami_levy")
    se.add_argument("--fixtures", help="offline: dir with index.json")
    se.add_argument("--unit", choices=["kg", "l", "unit", "m"], help="expected unit (e.g. unit for eggs)")
    se.add_argument("--grep", help="only show names containing this text")
    se.set_defaults(func=cmd_search)

    p = sub.add_parser("parse", help="show how free text resolves through the defaults (no network)")
    p.add_argument("--text")
    p.add_argument("--text-file")
    p.add_argument("--defaults", default=DEFAULT_DEFAULTS)
    p.set_defaults(func=cmd_parse)

    for name, hlp in (("add", "add items to the working list (free text)"), ("list", "show the working list"),
                      ("remove", "remove an item (name or 1-based index)"), ("clear", "empty the working list")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--defaults", default=DEFAULT_DEFAULTS)
        if name == "add":
            sp.add_argument("text", nargs="+")
        if name == "remove":
            sp.add_argument("item")
        sp.set_defaults(func=cmd_list_ops)

    lp = sub.add_parser("learn", help="learn_preference: record a correction as a default (with reason + date)")
    lp.add_argument("item")
    lp.add_argument("change", help='JSON dict, e.g. \'{"exclude":["נצמד"]}\', or a phrase: "לא X, קח Y"')
    lp.add_argument("--reason")
    lp.add_argument("--site", help="needed for pin phrases")
    lp.add_argument("--defaults", default=DEFAULT_DEFAULTS)
    lp.add_argument("--user-file")
    lp.set_defaults(func=cmd_learn)

    m = sub.add_parser("remember", help="upsert a default (future MCP tool remember_grocery_default)")
    m.add_argument("alias")
    m.add_argument("--defaults", default=DEFAULT_DEFAULTS)
    m.add_argument("--user-file")
    for k in ("terms", "brands", "aliases", "keywords", "fallback_terms", "extra_terms", "exclude_words", "require_any",
              "prefer_words"):
        m.add_argument("--" + k.replace("_", "-"), dest=k, help="comma-separated")
    m.add_argument("--qty", type=float)
    m.add_argument("--unit", choices=["kg", "l", "unit", "m"])
    m.add_argument("--produce", action="store_true")
    m.add_argument("--pack-preference", help="ordered pack sizes in the item's unit, e.g. 30,18,12")
    m.add_argument("--note")
    m.set_defaults(func=cmd_remember)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CloudflareBlocked as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 10
    except SessionExpired as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 11
    except RobotsDisallowed as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 12
    except (HttpError, CartError, ListError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
