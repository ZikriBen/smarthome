"""Reports: full Hebrew Markdown (file), compact one-line-per-item (for the agent), and site comparison."""
from __future__ import annotations

from typing import Dict, List, Optional

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
WEIGHT_QTY_CONFIRMED = {"shufersal", "rami_levy"}   # kg in links: Shufersal P_91:0.5, Rami 101,0.75 (Ben 19.10)
DIFF_MIN = 1.0
MIXED_MIN = 15.0
MAX_DIFFS = 8


def _pack_amount(p: Pick):
    c = p.chosen.p
    return None if c.by_weight else c.pack.amount


KASHRUT_UNKNOWN = "כשרות לא ידועה"


def check_reasons(site: str, p: Pick) -> List[str]:
    """Why an item of one site needs a look by hand (empty = fine). needs_choice items are listed on
    their own '❓ לבחירה' line instead."""
    if getattr(p, "needs_choice", False):
        return []
    if p.to_check:
        return [p.check_reason]
    out = []
    if not p.chosen.p.link_code:
        out.append("אין ברקוד — לא בקישור")
    if p.chosen.p.by_weight and site not in WEIGHT_QTY_CONFIRMED:
        out.append("כמות במשקל — בדוק בעגלה")
    for f in p.flags:
        if f.startswith("לא נמצא "):
            out.append(f)
    if p.confidence in ("low", "none") and not any(x.startswith("לא נמצא") for x in out):
        out.append("לא בטוח בהתאמה")
    return out


def _site_total(results_site):
    return sum(p.chosen.p.price * p.qty for p in results_site if not p.to_check)


def cart_message(results: Dict[str, List[Pick]], names: Dict[str, str], urls: Dict[str, List[str]],
                 site_errors: Dict[str, str] = None, skipped: Dict[str, str] = None,
                 generated: str = "", delivery: Dict = None) -> str:
    """results: {site: picks (same item order)}; urls: {site: [url, …]} (empty list = no link).
    delivery: {site: SiteDelivery} — when set, header shows items+fee=final and savings use final totals.
    site_errors / skipped: see plan §1-D. Totals only count items priced on every searched site."""
    from .delivery import SiteDelivery, final_total
    site_errors, skipped = site_errors or {}, skipped or {}
    delivery = delivery or {}
    sites = [s for s in results if s not in site_errors]
    n = len(next(iter(results.values()))) if results else 0
    ref = results[sites[0]] if sites else None
    head = f"🛒 {n} פריטים"
    diffs, near, mixed_gain = [], 0, 0.0
    tot = {s: 0.0 for s in sites}          # items-only (common items when comparing)
    fee_line = []                          # per-site "X + משלוח Y = Z" rows (always when delivery set)
    min_warn = []
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
    elif sites:
        s = sites[0]
        tot[s] = _site_total(results[s])
    # Final totals = items + delivery (savings and "cheapest" use these).
    finals = {}
    for s in sites:
        d = delivery.get(s) or SiteDelivery()
        items = tot[s]
        charge = d.charge(items)
        fin = final_total(items, d)
        finals[s] = fin
        if delivery:
            fee_txt = f"משלוח ₪{charge:.2f}" + (" (חינם)" if charge == 0 and d.fee else "")
            fee_line.append(f"{names.get(s, s)}: ₪{items:.2f} + {fee_txt} = ₪{fin:.2f}")
            short = d.shortfall(items)
            if short:
                min_warn.append(f"{names.get(s, s)}: חסרים ₪{short:.2f} למינימום ₪{d.min_order:g}")
    if len(sites) >= 2:
        best = min(sites, key=lambda s: finals[s])
        worst = max(sites, key=lambda s: finals[s])
        head += " · " + " · ".join(f"{names.get(s, s)} ₪{finals[s]:.2f}" for s in sites)
        if finals[worst] - finals[best] >= 0.01:
            head += f" (הכי זול: {names.get(best, best)}, חיסכון ₪{finals[worst] - finals[best]:.2f})"
        # Mixed basket pays BOTH delivery fees.
        if delivery:
            mixed_fees = sum((delivery.get(s) or SiteDelivery()).fee for s in sites)
            mixed_final = mixed + mixed_fees
            mixed_gain = finals[best] - mixed_final   # positive only if mixed (items+2 fees) beats best single
        else:
            mixed_gain = finals[best] - mixed
    elif sites:
        head += f" · {names.get(sites[0], sites[0])} ₪{finals[sites[0]]:.2f}"
    out = [head + (f" · הערכה {generated}" if generated else " · הערכה")]
    out.extend(fee_line)
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
        out.append(f"קנייה מעורבת (הזול בכל פריט) חוסכת עוד ₪{mixed_gain:.2f} — כולל שני משלוחים")
    for w in min_warn:
        out.append(f"⚠️ מינימום הזמנה — {w}")
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
    ask = []
    for i in range(n if ref else 0):
        per = [f"{SHORT.get(s, s)}: " + (f"הצעה {results[s][i].chosen.p.name} ₪{results[s][i].chosen.p.price:.2f}"
                                         if results[s][i].chosen and results[s][i].chosen.p.price is not None
                                         else "לא בעגלה")
               + f", {len(results[s][i].choices)} מועמדים"
               for s in sites if getattr(results[s][i], "needs_choice", False)]
        if per:
            ask.append(f"{ref[i].item.name} ({'; '.join(per)})")
    if ask:
        out.append("❓ לבחירה: " + " · ".join(ask))
    unk = []
    for s in sites:
        names_unk = [p.item.name for p in results[s] if not p.to_check and KASHRUT_UNKNOWN in p.flags]
        if names_unk:
            unk.append(f"{SHORT.get(s, s)}: {len(names_unk)} פריטים ({', '.join(names_unk[:6])}"
                       + ("…" if len(names_unk) > 6 else "") + ")")
    if unk:
        out.append(f"⚠️ {KASHRUT_UNKNOWN} (האתר לא מציין כשרות — בדוק באריזה): " + " · ".join(unk))
    out.append("תתחבר קודם לאתר באותו דפדפן. התשלום אצלך בלבד.")
    from .links import split_message
    return split_message("\n".join(out))


def _pack_label(p: Pick) -> str:
    c = p.chosen.p
    return qty_label(p) if c.by_weight else c.pack.label()


# ---------------- weekly basket (Super-Hist first + ONE other store) ----------------
SH_CART_URL = "https://super-hist.co.il/index.php?route=checkout/cart"
W_SHORT = {"super_hist": "הסופר", "rami_levy": "רמי", "shufersal": "שופרסל"}
MAX_PRICIER = 8


def _money(v) -> str:
    return "?" if v is None else f"₪{v:.2f}"


def _wqty(r) -> str:
    q = r.get("qty")
    if q is None:
        return ""
    return f' {q:g} ק"ג' if r.get("by_weight") else (f" ×{q:g}" if q != 1 else "")


def _pricier_lines(ev: dict, limit: int = MAX_PRICIER) -> List[str]:
    """Items where Super-Hist costs more, merged over stores: 'חלב (+₪0.80 מול רמי, +₪0.25 מול שופרסל)'."""
    by_item: Dict[str, List[str]] = {}
    order: List[str] = []
    for s, e in ev.items():
        for x in e["pricier"]:
            if x["item"] not in by_item:
                by_item[x["item"]] = []
                order.append(x["item"])
            by_item[x["item"]].append(f"+₪{x['diff']:.2f} מול {W_SHORT.get(s, s)}"
                                      + (" ליחידה" if x["basis"] == "unit_price" and
                                         abs(x["sh_cost"] - x["other_cost"] - x["diff"]) > 0.01 else ""))
    return [f"{it} ({', '.join(by_item[it])})" for it in order[:limit]] + (
        [f"ועוד {len(order) - limit}"] if len(order) > limit else [])


def weekly_message(res: dict, other_urls: List[str], generated: str = "", sh_cart: Optional[dict] = None,
                   runtime: Optional[dict] = None, history: Optional[str] = None,
                   mode: str = "price") -> str:
    """The ready Hebrew Telegram message: part 1 Super-Hist, part 2 ONE other store, evidence, 'לבדיקה'.
    res = weekly.compute(); other_urls = cart link(s) for part 2; sh_cart = the cart-build proposal result
    (mode "cart"); runtime = {"seconds", "requests": {site: n}}."""
    from .links import split_message
    sh, other = res["super_hist"], res.get("other")
    out = [f"🛒 סל שבועי · {res['items']} פריטים" + (f" · {generated}" if generated else "")]
    # ---- part 1
    if res.get("sh_error"):
        out.append(f"1️⃣ הסופר החברתי: לא זמין — {res['sh_error'][:90]}. כל הרשימה עברה לחנות השנייה.")
    else:
        fee = "" if sh["fee"] is None else (f" + משלוח {_money(sh['fee'])} = {_money(sh['final'])}")
        out.append(f"1️⃣ הסופר החברתי — {len(sh['lines'])} פריטים · {_money(sh['total'])}{fee}"
                   + ("" if sh["fee"] is not None else " (בלי משלוח)"))
        c = sh_cart or {}
        if mode == "cart" and c.get("status") == "pending_approval":
            out.append(f"🧺 {c.get('headline') or 'הצעה לעדכון העגלה'} — ממתין לאישור שלך. אחרי האישור: "
                       f"[עגלת הסופר החברתי]({SH_CART_URL})")
        elif mode == "cart" and c.get("status") == "no_changes":
            out.append(f"🧺 העגלה כבר מכילה הכל. [עגלת הסופר החברתי]({SH_CART_URL})")
        elif mode == "cart" and c.get("status"):
            out.append(f"🧺 לא בניתי עגלה: {str(c.get('text') or c['status'])[:120]}")
        for r in sh["lines"]:
            out.append(f"• {r['name']}{_wqty(r)} {_money(r['cost'])}" + (" (במשקל)" if r["by_weight"] else ""))
        if mode != "cart" or (sh_cart or {}).get("status") not in ("pending_approval", "no_changes"):
            out.append("(מצב מחירים בלבד: זו רשימת הפריטים להזמנה בסופר החברתי; העגלה באתר לא שונתה)")
    # ---- part 2
    if other:
        o = other
        alt = o.get("alternative")
        cmp_txt = ""
        if alt and o["compare"].get(alt, {}).get("found"):
            ca, cc = o["compare"][alt], o["compare"][o["site"]]
            cmp_txt = (f" (על {cc['common']} הפריטים המשותפים: {W_SHORT[o['site']]} {_money(cc['final'])} מול "
                       f"{W_SHORT[alt]} {_money(ca['final'])}, כולל משלוח)")
        out.append(f"2️⃣ {o['name']} — {len(o['lines'])} פריטים · {_money(o['items_total'])} + משלוח "
                   f"{_money(o['fee'])} = {_money(o['final'])}{cmp_txt}")
        text, note = LINK_TEXT.get(o["site"], (o["name"], ""))
        for k, u in enumerate(other_urls or [], 1):
            part = f" חלק {k}/{len(other_urls)}" if len(other_urls) > 1 else ""
            out.append(f"[{text}{part}]({u})  ← {note}")
        if o["lines"] and not other_urls:
            out.append(f"{o['name']}: אין פריטים לקישור")
        for r in o["lines"]:
            out.append(f"• {r['item']}: {r['name']}{_wqty(r)} {_money(r['cost'])} (בסופר: {r['why_not_sh']})")
    elif res.get("remainder"):
        out.append("2️⃣ אין חנות שנייה זמינה לשאר הפריטים (רמי לוי ושופרסל נכשלו/דולגו).")
    else:
        out.append("2️⃣ אין צורך בחנות שנייה — הכל נמצא בסופר החברתי.")
    total = sh["final"] + (other["final"] if other else 0)
    out.append(f"סה״כ משוער: {_money(total)}" + (" (שתי הזמנות)" if other and sh["lines"] else ""))
    # ---- evidence
    ev = {s: e for s, e in res["evidence"].items() if e["lines"]}
    if ev and sh["lines"]:
        parts = []
        for s, e in ev.items():
            sign = (f"חיסכון {_money(e['saving'])}" if e["saving"] > 0.004 else
                    f"הסופר יקר ב-{_money(-e['saving'])}" if e["saving"] < -0.004 else "שווה")
            parts.append(f"{W_SHORT[s]} {_money(e['other_total'])} על {e['lines']}/{e['of']} ({sign})")
        out.append(f"📊 אותם פריטים של הסופר ({_money(sh['total'])}) בחנויות האחרות: " + " · ".join(parts))
        packs = sorted({x for e in ev.values() for x in e.get("pack_differs", [])})
        if packs:
            out.append(f"   (אריזה בגודל אחר ב: {', '.join(packs[:6])} — הסכומים לפי מה שכל עגלה קונה; "
                       "\"יקר יותר\" לפי מחיר ליחידה)")
        pr = _pricier_lines(ev)
        out.append("   יקר יותר בסופר: " + " · ".join(pr) if pr else "   אף פריט לא יקר יותר בסופר.")
    # ---- to check / choices / kashrut
    checks = [f"{r['item']} (הסופר: {r['reason']})" for r in sh["check"]]
    checks += [f"{r['item']} ({W_SHORT.get(r.get('site'), '')}: {r['reason']}"
               + (f"; נמצא ב{W_SHORT[r['elsewhere']['site']]} {_money(r['elsewhere']['cost'])}"
                  if r.get("elsewhere") else "") + ")"
               for r in res["check"] if r.get("site") or r.get("reason") == "אין חנות חלופית זמינה"]
    if checks:
        out.append("⚠️ לבדיקה (לא בסה״כ): " + " · ".join(checks))
    if sh["choice"]:
        out.append("❓ לבחירה בסופר (לא בסה״כ): " + " · ".join(
            f"{r['item']} ({r['candidates']} מועמדים)" for r in sh["choice"]))
    unk = [r["item"] for r in sh["lines"] if r["kashrut_unknown"]] + \
          [r["item"] for r in (other or {}).get("lines", []) if r["kashrut_unknown"]]
    if unk:
        out.append(f"⚠️ {KASHRUT_UNKNOWN} (בדוק באריזה): " + ", ".join(unk[:8]) + ("…" if len(unk) > 8 else ""))
    if runtime:
        req = runtime.get("requests") or {}
        out.append(f"⏱️ {_fmt_secs(runtime.get('seconds'))} · בקשות: "
                   + " · ".join(f"{W_SHORT.get(s, s)} {n}" for s, n in req.items() if n is not None))
    if history:
        out.append(f"📈 {history}")
    out.append("לא הוזמן ולא שולם כלום; התשלום אצלך בלבד.")
    return split_message("\n".join(out))


def _fmt_secs(s) -> str:
    if s is None:
        return "?"
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d} דק׳" if s >= 60 else f"{s} שנ׳"


def weekly_full(res: dict, other_urls: List[str], generated: str = "", estimate: Optional[dict] = None,
                runtime: Optional[dict] = None, history: Optional[str] = None, mode: str = "price",
                sh_cart: Optional[dict] = None) -> str:
    """Markdown report (saved next to the per-site reports)."""
    sh, other = res["super_hist"], res.get("other")
    out = [f"# סל שבועי — הסופר החברתי קודם ({generated})", "",
           f"{res['items']} פריטים · מצב: {'בניית עגלה (באישור)' if mode == 'cart' else 'מחירים בלבד'}", "",
           "## 1. הסופר החברתי", ""]
    if res.get("sh_error"):
        out.append(f"לא זמין: {res['sh_error']}. כל הרשימה עברה לחנות השנייה.")
    out += ["| פריט | מוצר | כמות | מחיר | עלות |", "|---|---|---|---|---|"]
    for r in sh["lines"]:
        out.append(f"| {_cell(r['item'])} | {_cell(r['name'])} | {_wqty(r).strip() or '1'} | {_money(r['price'])} | "
                   f"{_money(r['cost'])} |")
    out += ["", f"**סה\"כ הסופר החברתי:** {_money(sh['total'])}"
            + (" (דמי משלוח לא ידועים — לא נכללו)" if sh["fee"] is None else f" + משלוח {_money(sh['fee'])}"), ""]
    if sh_cart:
        out += [f"עגלה: {sh_cart.get('status')} — {_cell(sh_cart.get('text') or '')}", ""]
    out += ["## 2. החנות השנייה (שאר הפריטים)", ""]
    if other:
        out.append(f"נבחרה **{other['name']}**: {_money(other['items_total'])} + משלוח {_money(other['fee'])} = "
                   f"**{_money(other['final'])}**.")
        for s, c in other["compare"].items():
            out.append(f"- {NAMES_HE.get(s, s)}: {c['found']} פריטים נמצאו; על {c['common']} המשותפים "
                       f"{_money(c['common_total'])} + משלוח = {_money(c['final'])}")
        out += ["", "| פריט | למה לא בסופר | מוצר | עלות |", "|---|---|---|---|"]
        for r in other["lines"]:
            out.append(f"| {_cell(r['item'])} | {_cell(r['why_not_sh'])} | {_cell(r['name'])} | {_money(r['cost'])} |")
        for k, u in enumerate(other_urls or [], 1):
            out.append(f"\nקישור {k}: {u}")
    else:
        out.append("אין (הכל בסופר החברתי, או שאין חנות חלופית זמינה).")
    out += ["", "## 3. בדיקת ההנחה \"הסופר תמיד זול יותר\"", ""]
    for s, e in res["evidence"].items():
        out.append(f"### מול {NAMES_HE.get(s, s)}")
        gap = ("הסופר זול ב-" + _money(e["saving"]) if e["saving"] > 0.004 else
               "הסופר יקר ב-" + _money(-e["saving"]) if e["saving"] < -0.004 else "שווה")
        out.append(f"{e['lines']} מתוך {e['of']} פריטי הסופר נמצאו גם שם: הסופר {_money(e['sh_total'])} מול "
                   f"{_money(e['other_total'])} ({gap}). זול בסופר: {e['sh_cheaper']}, "
                   f"יקר בסופר: {e['sh_pricier']}, שווה: {e['same']}.")
        if e["pricier"]:
            out += ["", "| פריט | הסופר | שם | יותר יקר ב | בסיס |", "|---|---|---|---|---|"]
            for x in e["pricier"]:
                out.append(f"| {_cell(x['item'])} | {_money(x['sh_cost'])} | {_money(x['other_cost'])} | "
                           f"₪{x['diff']:.2f} | {'מחיר ליחידה' if x['basis'] == 'unit_price' else 'מחיר בפועל'} |")
        out.append("")
    if not res["evidence"]:
        out.append("לא היה עם מה להשוות (רמי לוי/שופרסל לא זמינים).")
    out += ["", "## לבדיקה / לבחירה (לא בסה\"כ)", ""]
    rows = ([f"- {r['item']}: הסופר — {r['reason']}" for r in sh["check"]] +
            [f"- {r['item']}: הסופר — ❓ {r['candidates']} מועמדים" for r in sh["choice"]] +
            [f"- {r['item']}: {r.get('reason')} (לא בסופר: {r.get('why_not_sh')})" for r in res["check"]
             if r not in sh["check"]])
    out += rows or ["אין."]
    out += ["", "## זמן ריצה ובקשות", ""]
    if runtime:
        out.append(f"בפועל: {_fmt_secs(runtime.get('seconds'))}; בקשות: " +
                   ", ".join(f"{NAMES_HE.get(s, s)} {n}" for s, n in (runtime.get('requests') or {}).items()))
    if estimate:
        out.append(f"הערכה ל-{estimate['lines']} שורות (שלושת האתרים במקביל): {_fmt_secs(estimate['wall_seconds'])}; "
                   + ", ".join(f"{NAMES_HE[s]} {estimate[s]['requests']} בקשות ≈ {_fmt_secs(estimate[s]['seconds'])}"
                               for s in ("super_hist", "rami_levy", "shufersal")))
        if estimate.get("approval_write"):
            aw = estimate["approval_write"]
            out.append(f"אחרי אישור העגלה (ברקע): {aw['requests']} בקשות ≈ {_fmt_secs(aw['seconds'])}")
    if history:
        out += ["", history]
    out += ["", "לא הוזמן ולא שולם כלום. שינוי בעגלת הסופר החברתי רק אחרי אישור אחד שלך."]
    return "\n".join(out) + "\n"


NAMES_HE = {"super_hist": "הסופר החברתי", "rami_levy": "רמי לוי", "shufersal": "שופרסל"}
