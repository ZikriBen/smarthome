"""Shared search -> filter -> score -> pick logic (site-agnostic, works on SiteAdapter + Product)."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import normalize as N
from .http import CloudflareBlocked, HttpError, RobotsDisallowed, SessionExpired
from .model import Item, Product

W_NAME, W_PRICE, W_CAT, W_BRAND, W_PREFER = 50.0, 35.0, 15.0, 10.0, 6.0
PEN_UNIT_MISMATCH, PEN_SIZE, PEN_NO_UNITPRICE = 25.0, 30.0, 10.0


@dataclass
class Candidate:
    p: Product
    found_by: str
    name_score: float = 0.0
    score: float = 0.0
    reasons: List[str] = field(default_factory=list)

    def __getattr__(self, k):          # convenience: c.id, c.name, c.price, c.pack ...
        if k.startswith("__") or "p" not in self.__dict__:
            raise AttributeError(k)
        return getattr(self.__dict__["p"], k)

    def short(self) -> str:
        p = self.p
        if p.price is None:
            return f"{p.name} ({p.id})"
        return f"{p.name} ({p.id}) ₪{p.price:.2f} · {N.unit_price_label(p.unit_price, p.pack.unit)}"


@dataclass
class Pick:
    item: Item
    chosen: Optional[Candidate]
    alternatives: List[Candidate]
    qty: Optional[float]
    confidence: str                 # high | medium | low | none
    flags: List[str]
    terms_used: List[str]
    stats: Dict[str, int]
    site: str = ""
    notes: List[str] = field(default_factory=list)   # always shown (e.g. pack-preference fallback)
    error: Optional[str] = None     # a search for this item failed (network/HTTP) and nothing was found

    @property
    def to_check(self) -> bool:
        """'לבדיקה': no priced product -> NOT in the total; must be shown explicitly, never dropped silently."""
        return self.chosen is None or self.chosen.p.price is None or not self.qty

    @property
    def check_reason(self) -> str:
        if self.error:
            return f"שגיאת אחזור ({self.error})"
        if self.chosen is None:
            pid = self.item.pinned_for(self.site)
            return f"מוצר נעוץ {pid} לא נמצא" if pid else "לא נמצא מוצר מתאים"
        if self.chosen.p.price is None:
            return "אין מחיר"
        return "אין כמות"


def name_score(name: str, phrases: List[str]) -> float:
    """Best fraction of phrase tokens present in the product name (0..1)."""
    n = N.norm_text(name)
    best = 0.0
    for ph in phrases:
        toks = [t for t in N.norm_text(ph).split() if t]
        if toks:
            best = max(best, sum(1 for t in toks if t in n) / len(toks))
    return best


def category_check(p: Product, f: Dict[str, List[str]]):
    """Unknown category (None — e.g. Shufersal search results) passes; only known ids are filtered."""
    for key in ("departments", "groups", "sub_groups"):
        v = p.cats.get(key)
        if v is None:
            continue
        if v in f.get("exclude_" + key, []):
            return False, f"{key} {v} excluded"
        if f.get(key) and v not in f[key]:
            return False, f"{key} {v} not in {f[key]}"
    return True, ""


def word_filter(p: Product, item: Item):
    """Hard name filters, same for every site. Returns (passes, reason)."""
    nm = N.norm_text(p.name)
    for w in item.exclude_words:
        if N.norm_text(w) in nm:
            return False, f"excluded word {w!r}"
    if item.require_any:
        hay = N.norm_text(p.name + " " + (p.brand or ""))
        if not any(N.norm_text(w) in hay for w in item.require_any):
            return False, f"none of {item.require_any}"
    return True, ""


def has_word(word: str, text: str) -> bool:
    """Whole-word match (a stray Latin 'L' inside another token doesn't count)."""
    import re
    w = re.escape(N.norm_text(word))
    return re.search(r"(?<![\w])" + w + r"(?![\w])", N.norm_text(text)) is not None


FRACTIONAL_KG_SITES = {"rami_levy"}   # weighed qty = exact kg (2 decimals, min MIN_KG), no site step
MIN_KG = 0.1


def resolve_qty(item: Item, p: Product, produce_defaults: dict) -> (float, List[str]):
    """Final cart quantity for the chosen product + explanatory flags. See README 'Quantities'."""
    flags = []
    uw = item.unit_weight_kg or produce_defaults.get("unit_weight_kg", 0.15)
    if p.by_weight:
        if item.qty is None:
            # produce: default units × weight per unit (Ben: 5 × 0.15 = 0.75 kg, then the site's step);
            # other weighed goods (deli etc.): produce_defaults.kg
            kg = produce_defaults.get("units", 5) * uw if item.produce else produce_defaults.get("kg", 0.5)
        elif item.qty_unit == "kg":
            kg = item.qty
        elif item.qty_unit == "unit" or item.produce:
            kg = item.qty * uw
            flags.append(f"{item.qty:g} יח' ≈ {kg:g} ק\"ג (משקל יחידה {uw:g})")
        else:
            kg = item.qty
        if p.site in FRACTIONAL_KG_SITES:
            # Rami Levy basket link takes any kg amount (Ben verified 101,1 and 101,0.75 on 19.10.2026)
            q = max(MIN_KG, round(kg, 2))
        else:
            q = N.round_qty(kg, True, p.step)
        if abs(q - kg) > 1e-9:
            flags.append(f"כמות עוגלה ל-{q:g} ק\"ג" + ("" if p.site in FRACTIONAL_KG_SITES
                                                           else f" (צעד {p.step or 0.5:g})"))
        return q, flags
    if item.qty is None:
        n = produce_defaults.get("units", 5) if item.produce else 1
    elif item.qty_unit == "kg":
        if p.pack.unit == "kg" and p.pack.amount:
            n = math.ceil(item.qty / p.pack.amount - 1e-9)
        else:
            n = math.ceil(item.qty / uw - 1e-9)
        flags.append(f"{item.qty:g} ק\"ג → {n} יח'")
    else:
        n = item.qty
    return N.round_qty(n, False), flags


PACK_TOL_REL = 0.05        # weights/volumes/lengths: ±5% (e.g. "1030 גרם" vinegar matches 1000 ml)


def pack_matches(amount: Optional[float], target: float, unit: Optional[str]) -> bool:
    if amount is None:
        return False
    if unit == "unit":
        return abs(amount - target) < 0.5            # counts must match exactly
    return abs(amount - target) <= PACK_TOL_REL * target


def pref_term_label(v: float, unit: Optional[str]) -> str:
    """pack_preference value (as written: units / grams / ml / meters) -> search suffix ("30", "1 ק\"ג")."""
    if unit == "kg":
        return f"{v / 1000:g} ק\"ג" if v >= 1000 else f"{v:g} גרם"
    if unit == "l":
        return f"{v / 1000:g} ליטר" if v >= 1000 else f"{v:g} מ\"ל"
    if unit == "m":
        return f"{v:g} מטר"
    return f"{v:g}"


def pack_search_plan(item: Item) -> List[tuple]:
    """Ordered extra searches for a pack_preference item: [(term, size_index or None)].
    Auto term for the top size first ("ביצים 30"), then the item's explicit extra_terms, then auto terms
    for the remaining sizes. The caller stops as soon as the top size is found and caps the count."""
    if not item.pack_preference:
        return []
    base = item.terms[0] if item.terms else item.name
    auto = [(f"{base} {pref_term_label(v, item.unit)}", i) for i, v in enumerate(item.pack_preference)]
    seq = auto[:1] + [(t, None) for t in item.extra_terms] + auto[1:]
    seen, out = set(item.terms), []
    for t, i in seq:
        if t not in seen:
            seen.add(t)
            out.append((t, i))
    return out


def fmt_pack(v: float, unit: Optional[str]) -> str:
    if unit in ("kg", "l"):
        return f"{v * 1000:g}" + (" גרם" if unit == "kg" else " מ\"ל")
    if unit == "m":
        return f"{v:g} מטר"
    return f"{v:g}"


def apply_pack_preference(ranked: List["Candidate"], item: Item):
    """Ordered pack-size preference. Returns (chosen or None, flags, fallback_used).
    Relevant = name score >= 0.5 and not sold by weight. The first preferred size that exists wins;
    inside that size the cheapest unit price wins (score breaks ties). None found -> (None, flag, True)."""
    prefs = item.pack_preference_base()
    if not prefs:
        return None, [], False
    unit = item.unit
    relevant = [c for c in ranked if c.name_score >= 0.5 and not c.p.by_weight
                and (unit is None or c.p.pack.unit == unit)]
    labels = [fmt_pack(v, unit) for v in prefs]
    for i, target in enumerate(prefs):
        hits = [c for c in relevant if pack_matches(c.p.pack.amount, target, c.p.pack.unit)]
        if hits:
            # within the preferred size the cheapest per unit wins; score only breaks ties
            best = min(hits, key=lambda c: (c.p.unit_price if c.p.unit_price is not None else float("inf"), -c.score))
            flags = []
            if i > 0:
                flags.append(f"אין מארז {'/'.join(labels[:i])}, נבחר {labels[i]}")
            return best, flags, False
    return None, [f"אף גודל מועדף ({'/'.join(labels)}) לא נמצא — נבחר לפי הלוגיקה הרגילה"], True


class Picker:
    SITE_FATAL = (CloudflareBlocked, SessionExpired, RobotsDisallowed)   # stop the whole site, don't hammer

    def __init__(self, adapter, log: Callable[[str], None] = None, n_alternatives: int = 3,
                 produce_defaults: Optional[dict] = None, deep_size: Optional[int] = None,
                 max_extra_searches: int = 2):
        """deep_size: page size for pack_preference items (default adapter.deep_size, 40).
        max_extra_searches: cap on extra pack searches per pack_preference item (so <= 1 + 2 requests)."""
        self.adapter = adapter
        self.deep_size = deep_size if deep_size is not None else getattr(adapter, "deep_size", None)
        self.max_extra = max_extra_searches
        self.errors: Dict[str, str] = {}      # term -> short error text
        self.site = adapter.name
        self.log = log or (lambda m: None)
        self.n_alt = n_alternatives
        self.produce = produce_defaults or {"units": 5, "kg": 0.5, "unit_weight_kg": 0.15}
        self._cache: Dict[str, List[dict]] = {}
        self.search_count = 0

    def _search(self, term: str, size: Optional[int] = None) -> List[dict]:
        if term not in self._cache:
            self.log(f"[{self.site}] search: {term}" + (f" (size {size})" if size else ""))
            self.search_count += 1
            try:
                self._cache[term] = self.adapter.search(term, size=size) if size else self.adapter.search(term)
            except self.SITE_FATAL:
                raise
            except HttpError as e:
                # one failed search must not kill the run: the item becomes 'לבדיקה' (to check)
                self.errors[term] = str(e)[:120]
                self.log(f"[{self.site}] search FAILED: {term}: {self.errors[term]}")
                self._cache[term] = []
        return self._cache[term]

    def _gather(self, item: Item, terms: List[str], tag: str, stats: Counter,
                size: Optional[int] = None) -> List[Candidate]:
        f = item.site_filters(self.site)
        out: Dict[str, Candidate] = {}
        for t in terms:
            for raw in self._search(t, size):
                p = self.adapter.normalize(raw, item.unit)
                if p is None:
                    continue
                stats["seen"] += 1
                if not p.available:
                    stats["not_in_store"] += 1
                    continue
                ok, _ = category_check(p, f)
                if not ok:
                    stats["wrong_category"] += 1
                    continue
                ok, _ = word_filter(p, item)
                if not ok:
                    stats["excluded_word"] += 1
                    continue
                out.setdefault(p.id, Candidate(p, f"{tag}:{t}"))
        return list(out.values())

    def score(self, cands: List[Candidate], item: Item) -> None:
        has_filter = any(item.site_filters(self.site).get(k) for k in ("departments", "groups", "sub_groups"))
        for c in cands:
            c.name_score = name_score(c.p.name, item.match_words)
        unit = item.unit
        if not unit:
            pool = [c.p.pack.unit for c in cands if c.p.pack.unit and c.name_score >= 0.5] or \
                   [c.p.pack.unit for c in cands if c.p.pack.unit]
            unit = Counter(pool).most_common(1)[0][0] if pool else None
        comparable = [c.p.unit_price for c in cands
                      if c.p.unit_price and c.p.pack.unit == unit and c.name_score >= 0.5]
        best_up = min(comparable) if comparable else None
        brands = [N.norm_text(b) for b in item.brands]
        for c in cands:
            p = c.p
            s = W_NAME * c.name_score
            c.reasons = [f"name {c.name_score:.2f}"]
            s += W_CAT if has_filter else W_CAT / 2
            if has_filter:
                c.reasons.append("category ok")
            if p.unit_price and best_up and p.pack.unit == unit:
                s += W_PRICE * (best_up / p.unit_price)
                c.reasons.append(f"unit price ×{p.unit_price / best_up:.2f} of best")
            elif p.unit_price is None:
                s -= PEN_NO_UNITPRICE
                c.reasons.append("no unit price")
            if unit and p.pack.unit and p.pack.unit != unit:
                s -= PEN_UNIT_MISMATCH
                c.reasons.append(f"unit {p.pack.unit}≠{unit}")
            if p.pack.amount and not p.by_weight and (
                    (item.size_min is not None and p.pack.amount < item.size_min) or
                    (item.size_max is not None and p.pack.amount > item.size_max)):
                s -= PEN_SIZE
                c.reasons.append("pack size out of range")
            if brands:
                hay = N.norm_text(p.name + " " + p.brand)
                if any(b in hay for b in brands):
                    s += W_BRAND
                    c.reasons.append("preferred brand")
            if item.prefer_words:
                hits = sum(1 for w in item.prefer_words if has_word(w, p.name))
                if hits:
                    s += W_PREFER * hits / len(item.prefer_words)
                    c.reasons.append(f"attributes {hits}/{len(item.prefer_words)}")
            c.score = round(s, 2)

    @staticmethod
    def _confidence(c: Optional[Candidate], from_fallback: bool, n: int, item: Item):
        if c is None:
            return "none", ["לא נמצא מוצר מתאים"]
        flags = []
        if c.name_score >= 0.99:
            conf = "high"
        elif c.name_score >= 0.5:
            conf, _ = "medium", flags.append("התאמת שם חלקית")
        else:
            conf, _ = "low", flags.append("שם המוצר לא תואם את החיפוש")
        if c.p.unit_price is None:
            flags.append("אין מחיר ליחידה")
            conf = "medium" if conf == "high" else "low"
        if from_fallback:
            flags.append("נמצא רק במונח חלופי")
            conf = "medium" if conf == "high" else conf
        if item.source == "text":
            flags.append("אין ברירת מחדל לפריט — חיפוש חופשי")
            conf = "medium" if conf == "high" else conf
        if n == 1 and conf == "high":
            flags.append("מועמד יחיד")
        return conf, flags

    def _pref_found(self, cands: List[Candidate], item: Item, idx: int) -> bool:
        target = item.pack_preference_base()[idx]
        return any(c.name_score >= 0.5 and not c.p.by_weight and (item.unit is None or c.p.pack.unit == item.unit)
                   and pack_matches(c.p.pack.amount, target, c.p.pack.unit) for c in cands)

    def _pack_searches(self, item: Item, cands: List[Candidate], stats: Counter, size) -> List[str]:
        """Extra searches until the top preferred pack shows up (max self.max_extra). Merged by product id."""
        done: List[str] = []
        for term, idx in pack_search_plan(item):
            if len(done) >= self.max_extra or self._pref_found(cands, item, 0):
                break
            if idx is not None and self._pref_found(cands, item, idx):
                continue            # that size is already in the results
            done.append(term)
            known = {c.p.id for c in cands}
            cands += [c for c in self._gather(item, [term], "pack", stats, size) if c.p.id not in known]
            self.score(cands, item)
        return done

    def pick(self, item: Item) -> Pick:
        pick = self._pick(item)
        errs = [f"{t}: {self.errors[t]}" for t in pick.terms_used if t in self.errors]
        if errs:
            if pick.chosen is None:
                pick.error = "; ".join(errs)
                pick.flags = [f"שגיאת אחזור — לבדיקה ({pick.error})"]
                pick.confidence = "none"
            else:
                pick.flags.append("חלק מהחיפושים נכשלו: " + "; ".join(errs))
        return pick

    def _pick(self, item: Item) -> Pick:
        stats = Counter()
        terms_used = list(item.terms)
        size = self.deep_size if item.pack_preference and not item.pinned_for(self.site) else None
        cands = self._gather(item, item.terms, "term", stats, size)
        relaxed = False
        if not cands and stats.get("wrong_category") and any(item.site_filters(self.site).values()):
            # category ids may be stale/wrong: retry without the site filter, but flag it
            saved = item.filters.pop(self.site, None)
            try:
                cands = self._gather(item, item.terms, "term", Counter())
            finally:
                if saved is not None:
                    item.filters[self.site] = saved
            relaxed = bool(cands)
        self.score(cands, item)
        if size and cands:
            terms_used += self._pack_searches(item, cands, stats, size)
        ranked = sorted(cands, key=lambda c: c.score, reverse=True)
        from_fallback = False
        best = ranked[0] if ranked and ranked[0].name_score >= 0.5 else None
        if best is None and item.fallback_terms:
            terms_used += item.fallback_terms
            known = {c.p.id for c in cands}
            cands += [c for c in self._gather(item, item.fallback_terms, "fallback", stats) if c.p.id not in known]
            self.score(cands, item)
            ranked = sorted(cands, key=lambda c: c.score, reverse=True)
            best = ranked[0] if ranked else None
            from_fallback = bool(best and best.found_by.startswith("fallback"))
        elif best is None and ranked:
            best = ranked[0]

        pinned_id = item.pinned_for(self.site)
        pref_flags: List[str] = []
        if pinned_id:
            pinned = next((c for c in cands if c.p.id == pinned_id), None)
            if pinned is None:   # maybe filtered by category/words but in store
                for t in terms_used:
                    for raw in self._search(t):
                        p = self.adapter.normalize(raw, item.unit)
                        if p and p.id == pinned_id and p.available:
                            pinned = Candidate(p, f"pinned:{t}", name_score(p.name, item.match_words))
                            break
                    if pinned:
                        break
            if pinned is None:
                q = item.qty if item.qty is not None else 1
                return Pick(item, None, ranked[: self.n_alt], N.round_qty(q, False), "low",
                            [f"product_id {pinned_id} נעוץ אך לא נמצא בתוצאות/בסניף — ייכלל כמו שהוא"],
                            terms_used, dict(stats), self.site)
            best, conf, flags = pinned, "high", ["מוצר נעוץ (product_id)"]
        else:
            pref_fallback = False
            if item.pack_preference and ranked:
                chosen, pref_flags, pref_fallback = apply_pack_preference(ranked, item)
                if chosen is not None:
                    best = chosen
            conf, flags = self._confidence(best, from_fallback, len(cands), item)
            if best is not None:
                flags += pref_flags
                if pref_fallback:
                    conf = {"high": "medium", "medium": "low"}.get(conf, conf)
        if relaxed and best is not None:
            flags.append("סינון הקטגוריה הסיר הכל — נבחר בלי סינון")
            conf = "low" if conf != "none" else conf
        alts = [c for c in ranked if best is None or c.p.id != best.p.id][: self.n_alt]
        qty = None
        if best:
            qty, qflags = resolve_qty(item, best.p, self.produce)
            flags += qflags
        notes = [f for f in flags if f in pref_flags] if not pinned_id else []
        return Pick(item, best, alts, qty, conf, flags, terms_used, dict(stats), self.site, notes)


def build_plan(picks: List[Pick], site: str, store, meta: dict) -> dict:
    cart: Dict[str, float] = {}
    sell: Dict[str, str] = {}
    lines = []
    for p in picks:
        pid = p.chosen.p.id if p.chosen else p.item.pinned_for(site)
        line = {"item": p.item.name, "input": p.item.raw_text, "source": p.item.source, "product_id": pid,
                "qty": p.qty, "confidence": p.confidence, "flags": p.flags, "terms": p.terms_used,
                "to_check": p.to_check, "error": p.error}
        if p.chosen:
            c = p.chosen.p
            line.update({"product_name": c.name, "brand": c.brand, "price": c.price, "shelf_price": c.shelf_price,
                         "pack": c.pack.label(), "unit": c.pack.unit, "unit_price": c.unit_price,
                         "by_weight": c.by_weight, "promo": c.promo_note, "link_code": c.link_code,
                         "link_qty": p.qty,
                         "alternatives": [{"id": a.p.id, "name": a.p.name, "price": a.p.price,
                                           "unit_price": a.p.unit_price, "unit": a.p.pack.unit}
                                          for a in p.alternatives]})
            sell[c.id] = c.sell_method
        lines.append(line)
        if pid and p.qty:
            cart[pid] = round(cart.get(pid, 0) + p.qty, 3)
    est = sum((l.get("price") or 0) * (l.get("qty") or 0) for l in lines if l.get("price"))
    to_check = [{"item": p.item.name, "reason": p.check_reason, "error": p.error} for p in picks if p.to_check]
    return {"site": site, "store": str(store) if store else None, "cart": cart, "sell_methods": sell,
            "estimated_total": round(est, 2),
            "to_check": to_check,                       # NOT in estimated_total; always shown in reports
            "fetch_errors": [t["item"] for t in to_check if t["error"]],
            "unresolved": [l["item"] for l in lines if not l["product_id"]],
            "low_confidence": [l["item"] for l in lines if l["confidence"] in ("low", "none")],
            "lines": lines, "meta": meta}
