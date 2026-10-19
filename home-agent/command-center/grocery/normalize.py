"""Product normalization: pack size, unit price, promo notes, quantity steps."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

UNIT_LABEL_HE = {"kg": 'ק"ג', "l": "ליטר", "unit": "יח'", "m": "מטר"}

# normalized UOM token -> (base unit, factor to base)
_UOM = {
    "גרם": ("kg", 0.001), "גר": ("kg", 0.001), "ג": ("kg", 0.001), "g": ("kg", 0.001),
    "gr": ("kg", 0.001), "gram": ("kg", 0.001), "grams": ("kg", 0.001),
    "קג": ("kg", 1.0), "קילו": ("kg", 1.0), "קילוגרם": ("kg", 1.0), "kg": ("kg", 1.0),
    "מל": ("l", 0.001), "מיליליטר": ("l", 0.001), "ml": ("l", 0.001),
    "ליטר": ("l", 1.0), "ליטרים": ("l", 1.0), "ל": ("l", 1.0), "l": ("l", 1.0), "lt": ("l", 1.0),
    "liter": ("l", 1.0), "litre": ("l", 1.0),
    "יחידה": ("unit", 1.0), "יחידות": ("unit", 1.0), "יח": ("unit", 1.0), "unit": ("unit", 1.0),
    "units": ("unit", 1.0), "פיתות": ("unit", 1.0), "ביצים": ("unit", 1.0),
    "מטר": ("m", 1.0), "מטרים": ("m", 1.0), "מ": ("m", 1.0), "m": ("m", 1.0),
    "סמ": ("m", 0.01), "cm": ("m", 0.01),
}


def norm_text(s: str) -> str:
    """Normalize Hebrew text for matching: quotes/geresh, dashes, whitespace."""
    if not s:
        return ""
    s = str(s)
    s = s.replace("״", '"').replace("׳", "'").replace("’", "'").replace("`", "'")
    s = s.replace("-", " ").replace("־", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def norm_uom(uom: Optional[str]) -> Optional[Tuple[str, float]]:
    if not uom:
        return None
    t = str(uom).strip().lower()
    t = t.replace("\n", "").replace("\r", "")
    t = re.sub(r"[\"'״׳.\s]", "", t)
    return _UOM.get(t)


_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT_ALT = r'(ק"ג|קג|קילו|גרם|גר|ג|מ"ל|מל|ליטר|ל|מטר|מ|ס"מ|יחידות|יח|פיתות|ml|kg|g|l)'
_MULTI_RE = re.compile(_NUM + r"\s*[x×*]\s*" + _NUM + r"\s*" + _UNIT_ALT + r"?(?![א-תa-z])")
_MULTI_RE_REV = re.compile(_NUM + r"\s*" + _UNIT_ALT + r"\s*[x×*]\s*" + _NUM + r"(?![א-ת\d])")
_SINGLE_RE = re.compile(_NUM + r"\s*" + _UNIT_ALT + r"(?![א-תa-z])")


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def parse_name_sizes(name: str) -> List[Tuple[str, float, bool]]:
    """Extract (unit, total_amount, is_multipack) candidates from a product name."""
    n = norm_text(name).replace("×", "x")
    out = []
    taken = []
    for m in _MULTI_RE.finditer(n):
        count, each, uom = _f(m.group(1)), _f(m.group(2)), m.group(3)
        u = norm_uom(uom) if uom else None
        if u is None:  # "6x330" (cans) -> ml; "6x1.5" -> litres
            u = ("l", 0.001) if each >= 100 else ("l", 1.0)
        if count >= 1 and count <= 48:
            out.append((u[0], round(count * each * u[1], 6), True))
            taken.append(m.span())
    for m in _MULTI_RE_REV.finditer(n):
        each, uom, count = _f(m.group(1)), m.group(2), _f(m.group(3))
        u = norm_uom(uom)
        if u and 1 <= count <= 48:
            out.append((u[0], round(count * each * u[1], 6), True))
            taken.append(m.span())
    for m in _SINGLE_RE.finditer(n):
        if any(a <= m.start() < b for a, b in taken):
            continue
        u = norm_uom(m.group(2))
        if u:
            out.append((u[0], round(_f(m.group(1)) * u[1], 6), False))
    return out


@dataclass
class Pack:
    unit: Optional[str]          # kg / l / unit / m / None (unknown)
    amount: Optional[float]      # in base unit; 1.0 for by-weight items (price is per kg)
    by_weight: bool = False
    source: str = ""             # by_kilo | net_content | name | name_multipack | approx
    note: str = ""

    def label(self) -> str:
        if self.by_weight:
            return 'במשקל (לק"ג)'
        if not self.unit or not self.amount:
            return "?"
        a = self.amount
        if self.unit == "kg":
            return f'{a:g} ק"ג' if a >= 1 else f"{a * 1000:g} גרם"
        if self.unit == "l":
            return f"{a:g} ליטר" if a >= 1 else f'{a * 1000:g} מ"ל'
        if self.unit == "unit":
            return f"{a:g} יח'"
        return f"{a:g} מטר"


def net_content(raw: dict) -> Optional[Tuple[str, float]]:
    gs = raw.get("gs") or {}
    nc = gs.get("Net_Content") if isinstance(gs, dict) else None
    if not isinstance(nc, dict):
        return None
    u = norm_uom(nc.get("UOM"))
    val = nc.get("value")
    try:
        val = float(str(val).replace(",", ".")) if val not in (None, "") else None
    except ValueError:
        val = None
    if (u is None or val is None) and nc.get("text"):
        parsed = parse_name_sizes(str(nc.get("text")))
        if parsed:
            return parsed[0][0], parsed[0][1]
    if u is None or not val:
        return None
    return u[0], round(val * u[1], 6)


def is_by_weight(raw: dict) -> bool:
    prop = raw.get("prop") or {}
    return bool(prop.get("by_kilo")) or bool(prop.get("sw_shakil"))


def compute_pack(raw: dict, expected_unit: Optional[str] = None) -> Pack:
    if is_by_weight(raw):
        return Pack("kg", 1.0, by_weight=True, source="by_kilo")
    nc = net_content(raw)
    names = parse_name_sizes(raw.get("name") or "")
    multi = [x for x in names if x[2]]
    single = [x for x in names if not x[2]]

    def pick(unit):
        for src in (multi, single):
            for u, a, _ in src:
                if u == unit and a > 0:
                    return a, ("name_multipack" if src is multi else "name")
        return None

    if expected_unit:
        if expected_unit == "unit":
            # counts (eggs, pitas) usually only appear in the name; Net_Content is grams
            p = pick("unit")
            if p:
                return Pack("unit", p[0], source=p[1])
            if nc and nc[0] == "unit":
                return Pack("unit", nc[1], source="net_content")
        else:
            p_multi = next(((u, a) for u, a, _ in multi if u == expected_unit), None)
            if nc and nc[0] == expected_unit:
                if p_multi and p_multi[1] > nc[1] * 1.5:   # NC is per-unit inside a multipack
                    return Pack(expected_unit, p_multi[1], source="name_multipack")
                return Pack(expected_unit, nc[1], source="net_content")
            p = pick(expected_unit)
            if p:
                return Pack(expected_unit, p[0], source=p[1])
            # Quirk: liquids listed in grams (vinegar "1030 גרם") -> treat 1 kg ≈ 1 L
            if expected_unit == "l" and nc and nc[0] == "kg":
                return Pack("l", nc[1], source="approx", note="ק\"ג≈ליטר")
    # no expectation (or expectation unmet): best available
    if multi and nc and multi[0][0] == nc[0] and multi[0][1] > nc[1] * 1.5:
        return Pack(multi[0][0], multi[0][1], source="name_multipack")
    if nc:
        return Pack(nc[0], nc[1], source="net_content")
    if multi:
        return Pack(multi[0][0], multi[0][1], source="name_multipack")
    if single:
        return Pack(single[0][0], single[0][1], source="name")
    return Pack(None, None, source="unknown")


@dataclass
class Promo:
    label: str
    price: Optional[float]
    is_club: bool
    is_personal: bool
    unconditional: bool
    multi_buy: bool


_MULTIBUY_RE = re.compile(r"\d+\s*(?:ב|ב-|ב־|ב\s)")


def promos(raw: dict) -> List[Promo]:
    out = []
    for s in raw.get("sale") or []:
        if not isinstance(s, dict):
            continue
        label = str(s.get("label") or s.get("name") or "").strip()
        try:
            scm = float(s.get("scm")) if s.get("scm") not in (None, "") else None
        except (TypeError, ValueError):
            scm = None
        club = bool(s.get("is_club"))
        personal = bool(s.get("is_personal"))
        credit = "אשראי" in label
        multi = bool(_MULTIBUY_RE.search(label)) or bool(s.get("min_qty") and float(s.get("min_qty") or 0) > 1)
        out.append(Promo(label, scm, club, personal,
                         unconditional=not (club or personal or credit), multi_buy=multi))
    return out


def shelf_price(raw: dict) -> Optional[float]:
    p = raw.get("price")
    if isinstance(p, dict):
        p = p.get("price")
    try:
        return float(p) if p not in (None, "") else None
    except (TypeError, ValueError):
        return None


def effective_price(raw: dict) -> Tuple[Optional[float], Optional[Promo]]:
    """Shelf price, unless an unconditional single-item promo is cheaper (site-research pitfall 6)."""
    price = shelf_price(raw)
    best = None
    for pr in promos(raw):
        if pr.unconditional and not pr.multi_buy and pr.price and price and pr.price < price:
            if best is None or pr.price < best.price:
                best = pr
    return (best.price if best else price), best


def promo_note(raw: dict) -> str:
    notes = []
    for pr in promos(raw):
        tags = []
        if pr.is_club:
            tags.append("מועדון")
        if pr.is_personal:
            tags.append("אישי")
        if "אשראי" in pr.label:
            tags.append("אשראי")
        txt = pr.label or "מבצע"
        if pr.price and pr.label and str(pr.price) not in pr.label:
            txt += f" (₪{pr.price:.2f})"
        if tags:
            txt += " [" + ", ".join(dict.fromkeys(tags)) + "]"
        notes.append(txt)
    return "; ".join(notes)


def unit_price(price: Optional[float], pack: Pack) -> Optional[float]:
    if price is None or not pack.amount or not pack.unit:
        return None
    return round(price / pack.amount, 4)


def unit_price_label(up: Optional[float], unit: Optional[str]) -> str:
    if up is None or not unit:
        return "?"
    return f"₪{up:.2f}/{UNIT_LABEL_HE[unit]}"


def round_qty(qty: float, by_weight: bool, step: Optional[float] = None) -> float:
    """By-weight items: kg rounded UP to the step (default 0.5). Others: whole units, rounded up."""
    if by_weight:
        st = step if step and step > 0 else 0.5
        return round(max(st, math.ceil(round(qty / st, 6)) * st), 3)
    return float(max(1, math.ceil(qty - 1e-9)))


def cart_qty_str(qty: float) -> str:
    return f"{qty:.2f}"


def available_in_store(raw: dict, store) -> bool:
    av = raw.get("available_in")
    if not isinstance(av, list):
        return False
    s = str(store)
    return any(str(x) == s for x in av)
