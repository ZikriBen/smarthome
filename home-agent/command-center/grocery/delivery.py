"""Per-site home-delivery fee + minimum order (read from defaults.yaml, overridable).

Sources recorded 2026-10-19 (see defaults.yaml comments):
  Rami Levy  fee ₪35.90  — official page /he/orders-and-deliveries (NUC HTML: "עלות המשלוח - 35.90 ש\"ח").
             min 0       — no minimum on that page; hacoupon.co.il Oct 2026: "ללא דרישת סכום מינימום".
             note        — credit-club card holders may still pay the pre-raise ~29.90 (ynet May 2026).
  Shufersal  fee ₪35.90  — homepage (NUC HTML: "35.90 ₪ למשלוח"); ynet/TheMarker July 2026 raise from 29.90.
             min 0       — no site-level minimum found on current homepage; free-above-750 cancelled years ago.
Neither fee is area-dependent for the standard home-delivery product we model; Slot/club promos are
overrides Ben can set via grocery_learn_preference(change={"delivery": {site: {fee: …}}}).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

SITES = ("rami_levy", "shufersal")
DEFAULTS = {
    "rami_levy": {"fee": 35.90, "min_order": 0.0, "free_above": None},
    "shufersal": {"fee": 35.90, "min_order": 0.0, "free_above": None},
}


@dataclass(frozen=True)
class SiteDelivery:
    fee: float = 0.0
    min_order: float = 0.0
    free_above: Optional[float] = None   # None = never free; fee waived when items_total >= free_above
    note: str = ""

    def charge(self, items_total: float) -> float:
        if self.free_above is not None and items_total >= float(self.free_above):
            return 0.0
        return float(self.fee)

    def shortfall(self, items_total: float) -> float:
        need = float(self.min_order or 0)
        return max(0.0, need - items_total) if need else 0.0


def _num(v, default=0.0) -> float:
    if v in (None, "", "null"):
        return default
    return float(v)


def parse_site(raw: Optional[dict]) -> SiteDelivery:
    raw = raw or {}
    fa = raw.get("free_above")
    if fa in (None, "", "null"):
        fa = None
    else:
        fa = float(fa)
    return SiteDelivery(fee=_num(raw.get("fee")), min_order=_num(raw.get("min_order")),
                        free_above=fa, note=str(raw.get("note") or ""))


def load(raw: Optional[dict]) -> Dict[str, SiteDelivery]:
    """Merge DEFAULTS ← raw (defaults.yaml delivery:). Unknown sites are kept as given."""
    out = {s: parse_site(DEFAULTS[s]) for s in SITES}
    for s, cfg in (raw or {}).items():
        if isinstance(cfg, dict):
            base = out[s].__dict__ if s in out else {}
            out[str(s)] = parse_site({**base, **cfg})
    return out


def apply_override(current: Dict[str, SiteDelivery], change: dict) -> Dict[str, SiteDelivery]:
    """change = {site: {fee?, min_order?, free_above?, note?}} → new map (does not mutate)."""
    out = dict(current)
    for site, cfg in change.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"delivery[{site}] must be a mapping")
        base = out.get(site, SiteDelivery()).__dict__
        out[str(site)] = parse_site({**base, **cfg})
    return out


def final_total(items_total: float, d: SiteDelivery) -> float:
    return round(items_total + d.charge(items_total), 2)
