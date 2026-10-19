"""Shared data model: list items (intent) and normalized products (site-independent)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .normalize import Pack

UNITS = ("kg", "l", "unit", "m")
CAT_KEYS = ("departments", "exclude_departments", "groups", "exclude_groups",
            "sub_groups", "exclude_sub_groups")


@dataclass
class Item:
    """A resolved shopping intent (what to buy), independent of the site."""
    name: str
    qty: Optional[float] = None          # None = use defaults (1, or produce default)
    qty_unit: Optional[str] = None       # None | "unit" (count/packs) | "kg"
    terms: List[str] = field(default_factory=list)
    fallback_terms: List[str] = field(default_factory=list)
    extra_terms: List[str] = field(default_factory=list)      # pack_preference items: extra searches when the
                                                              # preferred pack isn't in the first results
    keywords: List[str] = field(default_factory=list)
    exclude_words: List[str] = field(default_factory=list)
    require_any: List[str] = field(default_factory=list)     # hard: name/brand must contain one of these
    prefer_words: List[str] = field(default_factory=list)    # soft attributes, e.g. "3%"
    brands: List[str] = field(default_factory=list)          # preferred brands (soft)
    product_id: Optional[str] = None                          # legacy: pinned id for the default site
    pinned: Dict[str, str] = field(default_factory=dict)      # {site: product_id}
    filters: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)  # {site: {departments:[..],..}}
    unit: Optional[str] = None
    size_min: Optional[float] = None
    size_max: Optional[float] = None
    pack_preference: List[float] = field(default_factory=list)  # ordered pack sizes in the item's unit
                                                                # (units / grams / ml / meters)
    produce: bool = False
    unit_weight_kg: Optional[float] = None
    note: Optional[str] = None
    source: str = "list"                 # list | defaults | text | unknown
    raw_text: Optional[str] = None

    @property
    def match_words(self) -> List[str]:
        return self.keywords or self.terms or [self.name]

    def site_filters(self, site: str) -> Dict[str, List[str]]:
        f = self.filters.get(site) or {}
        return {k: [str(x) for x in (f.get(k) or [])] for k in CAT_KEYS}

    def pack_preference_base(self) -> List[float]:
        """pack_preference converted to the base unit used by Pack (kg / l / unit / m)."""
        f = 0.001 if self.unit in ("kg", "l") else 1.0
        return [round(float(x) * f, 6) for x in self.pack_preference]

    def pinned_for(self, site: str) -> Optional[str]:
        return self.pinned.get(site) or (self.product_id if site == "rami_levy" else None)


@dataclass
class Product:
    """One search hit, normalized by a SiteAdapter."""
    site: str
    id: str
    name: str
    brand: str
    price: Optional[float]          # effective price used for comparison
    shelf_price: Optional[float]
    promo_used: Optional[str]
    promo_note: str
    pack: Pack
    unit_price: Optional[float]
    available: bool
    cats: Dict[str, Optional[str]]   # {"departments": id, "groups": id, "sub_groups": id}
    step: Optional[float] = None
    sell_method: str = "unit"        # unit | weight
    link_code: Optional[str] = None  # code used in cart links: Rami barcode (produce: 101/104), Shufersal "P_…"
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def by_weight(self) -> bool:
        return self.pack.by_weight
