"""Weekly basket: הסופר החברתי FIRST, the remainder at ONE other supermarket (pure logic, no network).

Assignment (per list item, same order on every site):
  * Super-Hist has a usable product (priced, in stock, confident, kosher rule OK)  -> part 1 (Super-Hist).
    "Usable" never depends on price: the default stays Super-Hist even where Rami/Shufersal is cheaper.
  * Super-Hist found something but is not sure (low confidence)                    -> part 1, 'לבדיקה'.
  * Super-Hist needs a choice among candidates                                     -> part 1, '❓ לבחירה'.
  * Not at Super-Hist (not found / out of stock / no acceptable kosher match / search failed / site down)
                                                                                    -> the remainder.
  The remainder goes to exactly ONE other store: whichever of Rami Levy / Shufersal is cheaper for it
  (items found at both + that store's delivery fee; a tie goes to the store that has more of the items).
  Never a per-item split between Rami and Shufersal. A remainder item missing at the chosen store is
  'לבדיקה' (shown, with the other store's price when it has it).
'לבדיקה' / '❓' lines are always shown and never counted in any total.

Evidence ("Super-Hist is always cheaper" is an assumption): every Super-Hist line is ALSO priced (read-only) at
Rami Levy and Shufersal; per store: the Super-Hist part's total vs the same items there, and the lines where
Super-Hist is more expensive and by how much (per-unit-price basis when packs differ, like compare_item).
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Dict, List, Optional

from . import kashrut as K
from .delivery import SiteDelivery, final_total
from .picker import Pick

SH = "super_hist"
OTHERS = ("rami_levy", "shufersal")
NAMES = {SH: "הסופר החברתי", "rami_levy": "רמי לוי", "shufersal": "שופרסל"}
KOSHER_FAIL = {f"לא נמצא {K.LEVEL_HE['mehadrin'].split(' /')[0]}", "לא נמצא בד״ץ"}
TIE = 0.05                    # ₪: |difference| below this is "same price"

# Request pacing (seconds between requests), used for the runtime estimate:
SH_DELAY = 10.0               # command_center.super_hist.MIN_INTERVAL
RAMI_DELAY = 1.5              # api.cart args.delay (Client adds 0-0.25 s jitter)
SHUF_DELAY = 10.0             # shufersal robots.txt Crawl-delay (floor)
SEARCHES_PER_LINE = 1.1       # fixtures: 14 searches for 13 lines (fallback terms, e.g. זירו -> פפסי מקס)
LATENCY = {SH: 1.0, "rami_levy": 0.5, "shufersal": 1.0}   # per request, rough


def kosher_failed(p: Optional[Pick]) -> bool:
    return bool(p) and any(f in KOSHER_FAIL for f in p.flags)


def usable(p: Optional[Pick]) -> bool:
    """Counts in a total: priced, has a qty, confident, not a pending choice, kosher rule OK."""
    return (p is not None and not p.to_check and not p.needs_choice and not kosher_failed(p)
            and p.confidence in ("high", "medium"))


def cost(p: Pick) -> float:
    return round(p.chosen.p.price * p.qty, 2)


def sh_state(p: Optional[Pick], sh_error: Optional[str]):
    """(state, reason): ok | check | choice | missing."""
    if sh_error or p is None:
        return "missing", "הסופר החברתי לא זמין" + (f" ({sh_error[:60]})" if sh_error else "")
    if p.needs_choice:
        return "choice", "❓ לבחירה"
    if p.error and p.chosen is None:
        return "missing", "שגיאת חיפוש"
    if p.chosen is None:
        return "missing", ("אזל במלאי" if (p.stats or {}).get("not_in_store") else "לא נמצא")
    if kosher_failed(p):
        return "missing", "אין התאמה כשרה"
    if p.to_check:
        return "missing", p.check_reason
    if p.confidence not in ("high", "medium"):
        return "check", "לא בטוח בהתאמה"
    return "ok", None


def line_of(p: Pick, site: str) -> Dict:
    c = p.chosen.p if p.chosen else None
    out = {"item": p.item.name, "site": site, "product_id": c.id if c else None,
           "name": c.name if c else None, "price": c.price if c else None, "qty": p.qty,
           "by_weight": bool(c and c.by_weight), "cost": cost(p) if c and c.price is not None and p.qty else None,
           "link_code": c.link_code if c else None, "url": (c.raw or {}).get("_url") if c else None,
           "unit_price": c.unit_price if c else None, "unit": c.pack.unit if c else None,
           "pack": c.pack.label() if c else None, "confidence": p.confidence,
           "kashrut_unknown": "כשרות לא ידועה" in p.flags,
           "minimum": (c.raw or {}).get("_minimum", 1) if c else 1}
    return out


def _cmp(a: Pick, b: Pick):
    """(diff ₪ a-b on a common basis, basis) — unit price × the smaller amount when units agree."""
    from .report import compare_item
    cmp = compare_item({"a": a, "b": b})
    if not cmp["winner"]:
        return None, None
    vals = cmp["norm"] if cmp["basis"] == "unit_price" else cmp["plan_cost"]
    return round(vals["a"] - vals["b"], 2), cmp["basis"]


def compute(results: Dict[str, List[Optional[Pick]]], site_errors: Dict[str, str] = None,
            delivery: Dict[str, SiteDelivery] = None) -> Dict:
    """results: {site: picks (same item order)} for super_hist / rami_levy / shufersal (any may be missing)."""
    site_errors = dict(site_errors or {})
    delivery = delivery or {}
    base = next((v for v in results.values() if v), [])
    n = len(base)
    sh_picks = results.get(SH) or [None] * n
    sh_error = site_errors.get(SH) if SH in results else (site_errors.get(SH) or "לא נבדק")
    others = [s for s in OTHERS if s in results and s not in site_errors]
    name_of = lambda i: next((r[i].item.name for r in results.values() if r and r[i] is not None), f"#{i}")

    sh_rows, sh_check, sh_choice, remainder = [], [], [], []
    states = []
    for i in range(n):
        p = sh_picks[i]
        st, why = sh_state(p, sh_error)
        states.append((st, why))
        if st == "ok":
            sh_rows.append(dict(line_of(p, SH), i=i))
        elif st == "check":
            sh_check.append(dict(line_of(p, SH), i=i, reason=why))
        elif st == "choice":
            sh_choice.append(dict(line_of(p, SH), i=i, reason=why, candidates=len(p.choices)))
        else:
            remainder.append((i, why))

    # ---- the ONE other store for the remainder
    per_store = {}
    for s in others:
        found = [i for i, _ in remainder if usable(results[s][i])]
        per_store[s] = {"found": found}
    common = [i for i, _ in remainder if all(i in per_store[s]["found"] for s in others)]
    for s in others:
        d = delivery.get(s) or SiteDelivery()
        items_common = round(sum(cost(results[s][i]) for i in common), 2)
        items_all = round(sum(cost(results[s][i]) for i in per_store[s]["found"]), 2)
        per_store[s].update(common_total=items_common,
                            compare_final=round(final_total(items_common, d), 2) if per_store[s]["found"] else 0.0,
                            items_total=items_all,
                            fee=round(d.charge(items_all), 2) if per_store[s]["found"] else 0.0)
        per_store[s]["final"] = round(per_store[s]["items_total"] + per_store[s]["fee"], 2)
    chosen = None
    if remainder and others:
        chosen = min(others, key=lambda s: (per_store[s]["compare_final"], -len(per_store[s]["found"]),
                                            OTHERS.index(s)))
    other = None
    check_rows = [dict(r) for r in sh_check]
    if chosen:
        ps = per_store[chosen]
        rows, missing = [], []
        for i, why in remainder:
            p = results[chosen][i]
            if usable(p):
                rows.append(dict(line_of(p, chosen), i=i, why_not_sh=why))
            else:
                alt = next((s for s in others if s != chosen and usable(results[s][i])), None)
                missing.append({"i": i, "item": name_of(i), "why_not_sh": why,
                                "reason": _other_reason(p),
                                "elsewhere": ({"site": alt, "cost": cost(results[alt][i])} if alt else None)})
        alt_s = [s for s in others if s != chosen]
        other = {"site": chosen, "name": NAMES[chosen], "lines": rows, "missing": missing,
                 "items_total": ps["items_total"], "fee": ps["fee"], "final": ps["final"],
                 "compare": {s: {"common": len(common), "common_total": per_store[s]["common_total"],
                                 "final": per_store[s]["compare_final"], "found": len(per_store[s]["found"])}
                             for s in others},
                 "alternative": alt_s[0] if alt_s else None}
        check_rows += [dict(m, site=chosen) for m in missing]
    elif remainder:
        check_rows += [{"i": i, "item": name_of(i), "why_not_sh": why, "reason": "אין חנות חלופית זמינה"}
                       for i, why in remainder]

    # ---- evidence: the Super-Hist lines priced at every other store
    evidence = {}
    for s in [x for x in OTHERS if x in results and x not in site_errors]:
        both = [r for r in sh_rows if usable(results[s][r["i"]])]
        sh_sum = round(sum(r["cost"] for r in both), 2)
        st_sum = round(sum(cost(results[s][r["i"]]) for r in both), 2)
        pricier, cheaper, same, packs = [], 0, 0, []
        for r in both:
            diff, basis = _cmp(sh_picks[r["i"]], results[s][r["i"]])
            o_c = results[s][r["i"]].chosen.p
            if basis == "unit_price" and not r["by_weight"] and o_c.pack.amount != sh_picks[r["i"]].chosen.p.pack.amount:
                packs.append(r["item"])
            if diff is None:
                diff, basis = round(r["cost"] - cost(results[s][r["i"]]), 2), "pack_price"
            if diff > TIE:
                pricier.append({"item": r["item"], "sh_cost": r["cost"], "other_cost": cost(results[s][r["i"]]),
                                "diff": diff, "basis": basis})
            elif diff < -TIE:
                cheaper += 1
            else:
                same += 1
        pricier.sort(key=lambda x: -x["diff"])
        evidence[s] = {"lines": len(both), "of": len(sh_rows), "sh_total": sh_sum, "other_total": st_sum,
                       "saving": round(st_sum - sh_sum, 2), "sh_cheaper": cheaper, "sh_pricier": len(pricier),
                       "same": same, "pricier": pricier, "pack_differs": packs}

    sh_total = round(sum(r["cost"] for r in sh_rows), 2)
    shd = delivery.get(SH)
    sh_fee = round(shd.charge(sh_total), 2) if shd and sh_rows else None
    return {"items": n, "sh_error": sh_error, "site_errors": site_errors,
            "super_hist": {"lines": sh_rows, "check": sh_check, "choice": sh_choice, "total": sh_total,
                           "fee": sh_fee, "final": round(sh_total + (sh_fee or 0), 2)},
            "remainder": [{"i": i, "item": name_of(i), "why_not_sh": why} for i, why in remainder],
            "other": other, "check": check_rows, "evidence": evidence,
            "states": [s for s, _ in states]}


def _other_reason(p: Optional[Pick]) -> str:
    if p is None:
        return "לא נבדק"
    if p.needs_choice:
        return "❓ לבחירה"
    if p.to_check:
        return p.check_reason
    if kosher_failed(p):
        return "אין התאמה כשרה"
    return "לא בטוח בהתאמה"


# --------------------------------------------------------------------------- runtime estimate

def estimate(lines: int, build_cart: bool = False, cart_changes: Optional[int] = None,
             searches_per_line: float = SEARCHES_PER_LINE) -> Dict:
    """Seconds and requests for one weekly run. The three sites run in PARALLEL (different hosts), so the wall
    time is the slowest site; every list line is searched on all three (the evidence section needs it).
      Super-Hist: 1 login check (account page) + searches, >=10 s apart.
      Rami Levy:  searches, 1.5 s apart.     Shufersal: searches, >=10 s apart (robots.txt Crawl-delay).
    build_cart adds, inside the run: 1 cart read (+1 mini-cart canary when empty) for the proposal; and AFTER
    Ben approves (background): 1 fresh login check + 1 cart read + 1 POST per change + 1 re-read."""
    searches = round(lines * searches_per_line)
    # +1 account-page re-check per LOGIN_CHECK_TTL (120 s) of searching, + the gate's check
    sh_req = 1 + searches + int(searches * (SH_DELAY + LATENCY[SH]) // 120) + (1 if build_cart else 0)
    out = {"lines": lines, "searches_per_site": searches,
           "super_hist": {"requests": sh_req, "seconds": round(sh_req * (SH_DELAY + LATENCY[SH]))},
           "rami_levy": {"requests": searches, "seconds": round(searches * (RAMI_DELAY + 0.125 + LATENCY["rami_levy"]))},
           "shufersal": {"requests": searches, "seconds": round(searches * (SHUF_DELAY + LATENCY["shufersal"]))}}
    out["wall_seconds"] = max(out[s]["seconds"] for s in (SH,) + OTHERS)
    out["requests_total"] = sum(out[s]["requests"] for s in (SH,) + OTHERS)
    # The old remainder-only design would run Rami/Shufersal AFTER Super-Hist on the remainder only.
    out["sequential_remainder_only_seconds"] = out[SH]["seconds"] + round(
        0.3 * lines * searches_per_line * (SHUF_DELAY + LATENCY["shufersal"]))
    if build_cart:
        k = cart_changes if cart_changes is not None else lines
        out["approval_write"] = {"requests": k + 3, "seconds": round((k + 3) * (SH_DELAY + LATENCY[SH]))}
    return out


def fmt_seconds(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d} דק׳" if s >= 60 else f"{s} שנ׳"


# --------------------------------------------------------------------------- history (assumption tracking)

def history_row(res: Dict, when: Optional[dt.datetime] = None, elapsed: Optional[float] = None,
                requests: Optional[Dict[str, int]] = None, mode: str = "price") -> Dict:
    when = when or dt.datetime.now().astimezone()
    o = res.get("other") or {}
    return {"date": when.isoformat(timespec="seconds"), "mode": mode, "items": res["items"],
            "super_hist": {"lines": len(res["super_hist"]["lines"]), "total": res["super_hist"]["total"]},
            "other": ({"site": o["site"], "lines": len(o["lines"]), "total": o["items_total"], "fee": o["fee"]}
                      if o else None),
            "to_check": len(res["check"]) + len(res["super_hist"]["choice"]),
            "evidence": {s: {k: e[k] for k in ("lines", "sh_total", "other_total", "saving", "sh_cheaper",
                                                "sh_pricier", "same")} for s, e in res["evidence"].items()},
            "site_errors": {s: str(v)[:80] for s, v in (res.get("site_errors") or {}).items()},
            "elapsed_seconds": round(elapsed) if elapsed is not None else None, "requests": requests or {}}


def history_append(path: str, row: Dict) -> None:
    """One JSON line per run (created on first use)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def history_read(path: str, last: int = 12) -> List[Dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(x) for x in f if x.strip()]
    return rows[-last:]


def history_summary(rows: List[Dict]) -> Optional[str]:
    """'4 ריצות: הסופר זול יותר ב-3 (חיסכון ממוצע ₪12.40 מול רמי לוי)…' or None."""
    if not rows:
        return None
    parts = []
    for s in OTHERS:
        ev = [r["evidence"][s] for r in rows if s in (r.get("evidence") or {}) and r["evidence"][s]["lines"]]
        if not ev:
            continue
        wins = sum(1 for e in ev if e["saving"] > TIE)
        avg = sum(e["saving"] for e in ev) / len(ev)
        parts.append(f"מול {NAMES[s]}: הסופר זול יותר ב-{wins}/{len(ev)} ריצות, ממוצע "
                     + (f"חיסכון ₪{avg:.2f}" if avg >= 0 else f"יקר ב-₪{-avg:.2f}"))
    return f"היסטוריה ({len(rows)} ריצות): " + " · ".join(parts) if parts else None
