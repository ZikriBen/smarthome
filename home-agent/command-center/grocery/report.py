"""Reports: full Hebrew Markdown (file), compact one-line-per-item (for the agent), and site comparison."""
from __future__ import annotations

from typing import Dict, List

from . import normalize as N
from .picker import Pick

CONF_HE = {"high": "✅ גבוה", "medium": "🟡 בינוני", "low": "⚠️ נמוך", "none": "❌ לא נמצא"}
CONF_ICON = {"high": "✅", "medium": "🟡", "low": "⚠️", "none": "❌"}


def _cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def qty_label(p: Pick) -> str:
    if p.qty is None:
        return "-"
    if p.chosen and p.chosen.p.by_weight:
        return f'{p.qty:g} ק"ג'
    return f"{p.qty:g}"


CHECK_ICON = "🔍"


def check_list(picks: List[Pick]) -> List[Pick]:
    return [p for p in picks if p.to_check]


def excludes_line(picks: List[Pick]) -> str:
    """'' when nothing is missing; else the explicit 'total excludes N items to check' sentence."""
    tc = check_list(picks)
    if not tc:
        return ""
    return (f"הסכום אינו כולל {len(tc)} פריטים לבדיקה: " +
            ", ".join(f"{p.item.name} ({p.check_reason})" for p in tc))


def meta_line(plan: dict) -> str:
    m = plan.get("meta") or {}
    bits = []
    if m.get("requests") is not None:
        bits.append(f"בקשות HTTP: {m['requests']}")
    if m.get("crawl_delay"):
        bits.append(f"השהיה בין בקשות: {m['crawl_delay']:g} ש' (robots.txt)")
    if m.get("robots_note"):
        bits.append(m["robots_note"])
    return "- " + "  ·  ".join(bits) if bits else ""


def render(picks: List[Pick], plan: dict, mode: str, site_name: str = "") -> str:
    out = [f"# סל קניות — {site_name or plan['site']} (POC)", ""]
    store = f"סניף (store): **{plan['store']}**  ·  " if plan.get("store") else ""
    out.append(f"- {store}מצב: **{mode}**  ·  נוצר: {plan['meta'].get('generated_at')}")
    out.append(f"- הערכת סכום (מחיר × כמות): **₪{plan['estimated_total']:.2f}** — פריטים במשקל יחויבו לפי שקילה בפועל.")
    exc = excludes_line(picks)
    if exc:
        out.append(f"- {CHECK_ICON} **{exc}**")
    ml = meta_line(plan)
    if ml:
        out.append(ml)
    if mode.startswith("fixtures"):
        out.append("- ⚠️ **נתונים מדומים/שמורים (fixtures) — המחירים אינם חיים.**")
    out += ["", "| # | פריט | מוצר נבחר | אריזה | מחיר | מחיר ליחידה | כמות | מבצע | ביטחון | חלופות |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for i, p in enumerate(picks, 1):
        alts = "<br>".join(_cell(a.short()) for a in p.alternatives) or "-"
        conf = CONF_HE.get(p.confidence, p.confidence)
        if p.to_check:
            conf = f"{CHECK_ICON} לבדיקה — לא בסכום"
        if p.flags:
            conf += "<br>" + "<br>".join(_cell(f) for f in p.flags)
        item = _cell(p.item.name) + (f"<br><sub>«{_cell(p.item.raw_text)}»</sub>" if p.item.raw_text and p.item.raw_text != p.item.name else "")
        if p.chosen:
            c = p.chosen.p
            price = f"₪{c.price:.2f}" if c.price is not None else "?"
            if c.promo_used and c.shelf_price:
                price += f" (מדף ₪{c.shelf_price:.2f})"
            row = [i, item, f"{_cell(c.name)} `{c.id}`", _cell(c.pack.label() + (f" ({c.pack.note})" if c.pack.note else "")),
                   price, N.unit_price_label(c.unit_price, c.pack.unit), qty_label(p), _cell(c.promo_note or "-"), conf, alts]
        else:
            pid = p.item.pinned_for(p.site)
            row = [i, item, f"`{pid}`" if pid else "-", "-", "-", "-", qty_label(p), "-", conf, alts]
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    out.append("")
    tc = check_list(picks)
    if tc:
        out.append(f"**{CHECK_ICON} לבדיקה ({len(tc)}) — לא נכללו בסכום:** " +
                   ", ".join(f"{p.item.name} ({p.check_reason})" for p in tc))
    if plan["unresolved"]:
        out.append("**לא נפתרו:** " + ", ".join(plan["unresolved"]))
    if plan["low_confidence"]:
        out.append("**לבדיקה ידנית (ביטחון נמוך):** " + ", ".join(plan["low_confidence"]))
    out += ["", "השוואה לפי מחיר מדף; מבצעי מועדון/אשראי/אישיים/כמותיים מוצגים בלבד ולא נכנסים לחישוב.",
            "לא בוצע שום שינוי בעגלה. (שלב 2 מנוטרל כברירת מחדל.)"]
    return "\n".join(out) + "\n"


def compact(picks: List[Pick], plan: dict, site_name: str = "") -> str:
    """One line per item, for the agent (Hermes)."""
    lines = []
    for p in picks:
        icon = CONF_ICON.get(p.confidence, "?")
        if p.chosen:
            c = p.chosen.p
            pr = f"₪{c.price:.2f}" if c.price is not None else "?"
            lines.append(f"{icon} {p.item.name} → {c.name} [{c.id}] ×{qty_label(p)} {pr} "
                         f"({N.unit_price_label(c.unit_price, c.pack.unit)})"
                         + (f" ⚑ {'; '.join(p.flags)}" if p.flags and p.confidence != 'high'
                            else (f" ⚑ {'; '.join(p.notes)}" if p.notes else "")))
        else:
            lines.append(f"{CHECK_ICON} {p.item.name} → לבדיקה: {p.check_reason}")
    tc = check_list(picks)
    head = f"{site_name or plan['site']}: {len(picks)} פריטים, הערכה ₪{plan['estimated_total']:.2f}"
    if tc:
        head += f" (לא כולל {len(tc)} לבדיקה: {', '.join(p.item.name for p in tc)})"
    low = [n for n in plan["low_confidence"] if n not in {p.item.name for p in tc}]
    if low:
        head += f", ביטחון נמוך: {', '.join(low)}"
    return head + "\n" + "\n".join(lines)


TIE_ABS, TIE_REL = 0.05, 0.01     # tie if within ₪0.05 or 1% of the cheaper normalized cost


def _amount(p: Pick):
    """Amount the plan actually buys, in the product's base unit (kg / l / unit / m)."""
    c = p.chosen.p
    if c.by_weight:
        return p.qty
    return (c.pack.amount or 0) * p.qty if c.pack.amount else None


def compare_item(picks: Dict[str, Pick]) -> dict:
    """Per-item comparison on UNIT PRICE.
    ref = the smallest amount any site's plan buys (same unit everywhere); normalized cost = unit price × ref.
    The plan cost (price × qty, what you actually pay) is reported separately and summed for the totals."""
    found = {s: p for s, p in picks.items() if p.chosen and p.chosen.p.price is not None and p.qty}
    out = {"plan_cost": {s: round(p.chosen.p.price * p.qty, 2) for s, p in found.items()},
           "basis": None, "winner": None, "ref": None, "norm": {}}
    if len(found) < len(picks) or len(found) < 2:
        return out
    units = {p.chosen.p.pack.unit for p in found.values()}
    amounts = {s: _amount(p) for s, p in found.items()}
    known = [a for a in amounts.values() if a]
    if len(units) == 1 and None not in units and all(p.chosen.p.unit_price for p in found.values()) and known:
        ref = min(known)          # a site whose pack size is unknown still has a unit price (e.g. Shufersal ppu)
        norm = {s: round(p.chosen.p.unit_price * ref, 2) for s, p in found.items()}
        out.update(basis="unit_price", ref=ref, unit=next(iter(units)), norm=norm)
        vals = norm
    else:
        out["basis"] = "pack_price"          # units differ / unknown: fall back to what the plan costs
        vals = out["plan_cost"]
    lo = min(vals.values())
    near = [s for s, v in vals.items() if v - lo <= max(TIE_ABS, TIE_REL * lo)]
    out["winner"] = "tie" if len(near) > 1 else near[0]
    return out


def _ref_label(ref, unit):
    if unit == "kg":
        return f'{ref:g} ק"ג' if ref >= 1 else f"{ref * 1000:g} גרם"
    if unit == "l":
        return f"{ref:g} ליטר" if ref >= 1 else f'{ref * 1000:g} מ"ל'
    return f"{ref:g} " + {"unit": "יח'", "m": "מטר"}.get(unit, "")


def compare(results: Dict[str, List[Pick]], names: Dict[str, str]) -> str:
    """results: {site: picks (same item order)}."""
    sites = list(results)
    n = len(next(iter(results.values())))
    out = ["# השוואת סל בין אתרים", "",
           "| פריט | " + " | ".join(names.get(s, s) for s in sites) + " | השוואה (לפי מחיר ליחידה) | זול יותר |",
           "|---|" + "---|" * len(sites) + "---|---|"]
    totals = {s: 0.0 for s in sites}
    all_found = {s: 0.0 for s in sites}
    wins = {s: 0 for s in sites}
    wins["tie"] = 0
    common = 0
    checks = []
    for i in range(n):
        picks = {s: results[s][i] for s in sites}
        cmp = compare_item(picks)
        row = [results[sites[0]][i].item.name]
        for s in sites:
            p = picks[s]
            if s in cmp["plan_cost"]:
                c = p.chosen.p
                all_found[s] += cmp["plan_cost"][s]
                how = qty_label(p) if c.by_weight else f"{qty_label(p)} × {c.pack.label()}"
                row.append(f"{_cell(c.name)}<br>₪{cmp['plan_cost'][s]:.2f} = {how} · "
                           f"{N.unit_price_label(c.unit_price, c.pack.unit)}"
                           + ("" if p.confidence in ("high", "medium") else " ⚠️"))
            else:
                row.append(f"{CHECK_ICON} לבדיקה: {_cell(p.check_reason)}")
        if cmp["winner"]:
            common += 1
            for s in sites:
                totals[s] += cmp["plan_cost"][s]
            wins[cmp["winner"]] += 1
            if cmp["basis"] == "unit_price":
                row.append(f"ל-{_ref_label(cmp['ref'], cmp['unit'])}: " +
                           " · ".join(f"{names.get(s, s)} ₪{cmp['norm'][s]:.2f}" for s in sites))
            else:
                row.append("לפי מחיר בפועל (יחידות שונות)")
            row.append("תיקו" if cmp["winner"] == "tie" else names.get(cmp["winner"], cmp["winner"]))
        else:
            missing = [s for s in sites if s not in cmp["plan_cost"]]
            checks.append((row[0], missing, picks))
            row += ["-", "לא נספר (לבדיקה ב" + ", ".join(names.get(s, s) for s in missing) + ")"]
        out.append("| " + " | ".join(row) + " |")
    out.append("")
    out.append(f"**סה\"כ בפועל (סכום מחירי העגלה, מחיר × כמות) על {common} פריטים שנמצאו בכל האתרים:** " +
               " · ".join(f"{names.get(s, s)} ₪{totals[s]:.2f}" for s in sites))
    if any(abs(all_found[s] - totals[s]) > 0.001 for s in sites):
        out.append("סה\"כ כולל פריטים שנמצאו רק באתר אחד: " +
                   " · ".join(f"{names.get(s, s)} ₪{all_found[s]:.2f}" for s in sites))
    out.append("ניצחונות לפי מחיר ליחידה: " + " · ".join(f"{names.get(s, s)} {wins[s]}" for s in sites) +
               f" · תיקו {wins['tie']}")
    if checks:
        out.append(f"**{CHECK_ICON} {len(checks)} פריטים לבדיקה — לא נספרו בניצחונות ולא בסה\"כ המשותף:** " +
                   "; ".join(f"{name} — " + ", ".join(f"{names.get(s, s)}: {picks[s].check_reason}" for s in miss)
                             for name, miss, picks in checks))
    out.append("")
    out.append("**איך להשוות:** \"זול יותר\" נקבע לפי מחיר ליחידה (₪ לק\"ג/ליטר/יח'/מטר) כפול אותה כמות השוואה — "
               "הכמות הקטנה ביותר שאחת העגלות קונה בפועל — כך שגודל אריזה לא מטעה. "
               f"תיקו = הפרש עד ₪{TIE_ABS:.2f} או {TIE_REL:.0%}. "
               "הסה\"כ הוא מה שהעגלות עולות בפועל (מחיר אריזה × כמות אריזות), ולכן אריזה גדולה יותר מייקרת את הסה\"כ "
               "גם כשהיא זולה ליחידה. פריטים במשקל — לפי שקילה בפועל.")
    return "\n".join(out) + "\n"


def compare_compact(results: Dict[str, List[Pick]], names: Dict[str, str]) -> str:
    sites = list(results)
    n = len(next(iter(results.values())))
    tot = {s: 0.0 for s in sites}
    lines = []
    checks = 0
    for i in range(n):
        cmp = compare_item({s: results[s][i] for s in sites})
        name = results[sites[0]][i].item.name
        if not cmp["winner"]:
            miss = [s for s in sites if s not in cmp["plan_cost"]]
            checks += 1
            lines.append(f"{CHECK_ICON} {name}: לבדיקה ב" + ", ".join(
                f"{names.get(s, s)} ({results[s][i].check_reason})" for s in miss) + " — לא נספר")
            continue
        for s in sites:
            tot[s] += cmp["plan_cost"][s]
        w = "תיקו" if cmp["winner"] == "tie" else names.get(cmp["winner"], cmp["winner"])
        lines.append(f"{name}: {w} (" + " / ".join(f"₪{cmp['plan_cost'][s]:.2f}" for s in sites) + ")")
    head = "סה\"כ בפועל: " + " · ".join(f"{names.get(s, s)} ₪{tot[s]:.2f}" for s in sites)
    if checks:
        head += f" (לא כולל {checks} פריטים לבדיקה — לא נספרו בסה\"כ ובניצחונות)"
    return head + "\n" + "\n".join(lines)


# ---------------- cart message (Telegram text for "תכין עגלה") ----------------
SHORT = {"rami_levy": "רמי", "shufersal": "שופרסל"}
LINK_TEXT = {"rami_levy": ("פתח עגלה ברמי לוי", "מחליף את העגלה בדפדפן"),
             "shufersal": ("פתח רשימה בשופרסל", "לחץ \"הוסף לסל\"; מוסיף לעגלה הקיימת")}
WEIGHT_QTY_CONFIRMED = {"shufersal"}       # Rami weighed qty semantics not yet confirmed by Ben
DIFF_MIN = 1.0
MIXED_MIN = 15.0
MAX_DIFFS = 8


def _pack_amount(p: Pick):
    c = p.chosen.p
    return None if c.by_weight else c.pack.amount


def check_reasons(site: str, p: Pick) -> List[str]:
    """Why an item of one site needs a look by hand (empty = fine)."""
    if p.to_check:
        return [p.check_reason]
    out = []
    if not p.chosen.p.link_code:
        out.append("אין ברקוד — לא בקישור")
    if p.chosen.p.by_weight and site not in WEIGHT_QTY_CONFIRMED:
        out.append("כמות במשקל — בדוק בעגלה")
    if p.confidence in ("low", "none"):
        out.append("לא בטוח בהתאמה")
    return out


def cart_message(results: Dict[str, List[Pick]], names: Dict[str, str], urls: Dict[str, List[str]],
                 site_errors: Dict[str, str] = None, skipped: Dict[str, str] = None,
                 generated: str = "") -> str:
    """results: {site: picks (same item order)}; urls: {site: [url, …]} (empty list = no link).
    site_errors: {site: message} for a site that failed/blocked during the run; skipped: {site: why} for a
    site not searched at all (circuit breaker). Totals only count items priced on every searched site."""
    site_errors, skipped = site_errors or {}, skipped or {}
    sites = [s for s in results if s not in site_errors]
    n = len(next(iter(results.values()))) if results else 0
    ref = results[sites[0]] if sites else None
    head = f"🛒 {n} פריטים"
    diffs, near, mixed_gain = [], 0, 0.0
    tot = {s: 0.0 for s in sites}
    if len(sites) >= 2:
        mixed = 0.0
        for i in range(n):
            picks = {s: results[s][i] for s in sites}
            cmp = compare_item(picks)
            if not cmp["winner"]:
                continue
            pc = cmp["plan_cost"]
            for s in sites:
                tot[s] += pc[s]
            mixed += min(pc.values())
            lo_s = min(pc, key=pc.get)
            gap = max(pc.values()) - min(pc.values())
            packs = {_pack_amount(p) for p in picks.values()}
            if gap >= DIFF_MIN or len(packs) > 1:
                order = sorted(sites, key=lambda s: pc[s])
                txt = f"{results[sites[0]][i].item.name}: " + " / ".join(
                    f"{SHORT.get(s, s)} ₪{pc[s]:.2f}" + (f" ({_pack_label(picks[s])})" if len(packs) > 1 else "")
                    for s in order)
                if cmp["basis"] == "unit_price" and cmp["winner"] not in ("tie", lo_s):
                    txt += f" — ליחידה: {SHORT.get(cmp['winner'], cmp['winner'])}"
                diffs.append((gap, txt))
            else:
                near += 1
        best = min(sites, key=lambda s: tot[s])
        worst = max(sites, key=lambda s: tot[s])
        head += " · " + " · ".join(f"{names.get(s, s)} ₪{tot[s]:.2f}" for s in sites)
        if tot[worst] - tot[best] >= 0.01:
            head += f" (הכי זול: {names.get(best, best)}, חיסכון ₪{tot[worst] - tot[best]:.2f})"
        mixed_gain = tot[best] - mixed
    elif sites:
        s = sites[0]
        tot[s] = sum(p.chosen.p.price * p.qty for p in results[s] if not p.to_check)
        head += f" · {names.get(s, s)} ₪{tot[s]:.2f}"
    out = [head + (f" · הערכה {generated}" if generated else " · הערכה")]
    for s in results:
        if s in site_errors:
            out.append(f"{names.get(s, s)}: נחסם/נכשל — לא נשלחו בקשות נוספות ({site_errors[s][:80]})")
            continue
        su = urls.get(s) or []
        text, note = LINK_TEXT.get(s, (names.get(s, s), ""))
        if not su:
            out.append(f"{names.get(s, s)}: אין פריטים לקישור")
        for k, u in enumerate(su, 1):
            part = f" חלק {k}/{len(su)}" if len(su) > 1 else ""
            out.append(f"[{text}{part}]({u})  ← {note}")
    for s, why in skipped.items():
        out.append(f"{names.get(s, s)}: דילגתי — {why}")
    if diffs:
        out.append("הבדלים:")
        diffs.sort(key=lambda d: -d[0])
        out += [d[1] for d in diffs[:MAX_DIFFS]]
        rest = near + max(0, len(diffs) - MAX_DIFFS)
        if rest:
            out.append(f"עוד {rest} פריטים כמעט זהים")
    if mixed_gain >= MIXED_MIN:
        out.append(f"קנייה מעורבת (הזול בכל פריט) חוסכת עוד ₪{mixed_gain:.2f} — אבל שני משלוחים")
    checks = []
    for i in range(n if ref else 0):
        name = ref[i].item.name
        why = []
        for s in sites:
            for r in check_reasons(s, results[s][i]):
                why.append(f"{SHORT.get(s, s)}: {r}" if len(sites) > 1 else r)
        if why:
            checks.append(f"{name} ({'; '.join(why)})")
    if checks:
        out.append("⚠️ לבדיקה: " + " · ".join(checks))
    out.append("תתחבר קודם לאתר באותו דפדפן. התשלום אצלך בלבד.")
    from .links import split_message
    return split_message("\n".join(out))


def _pack_label(p: Pick) -> str:
    c = p.chosen.p
    return qty_label(p) if c.by_weight else c.pack.label()
