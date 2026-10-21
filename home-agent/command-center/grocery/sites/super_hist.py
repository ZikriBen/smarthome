"""Histadrut "הסופר החברתי" (super-hist.co.il) adapter: read-only SEARCH for the grocery picker.

The site is reached ONLY through Command Center's `command_center.super_hist` module (login session, URL policy,
>=10 s between requests, 24 h breaker on 403/429/challenge). This vendored package never talks to the site by
itself: Command Center injects a transport with `set_transport(fn)`; fn(term) -> the raw `data` list of one
POST /search-results/ response (keys copied from the live run 2026-10-20, see tests/fixtures/super-hist):
  product_id, name, price "₪7.10" (SHELF), special "₪6.20" | false (the price actually paid), minimum,
  out_of_stock bool, weight bool + weight_unit_price_text (by-weight), in_cart bool, href, bgfnribbon_text.
The transport raises grocery.http errors: SessionExpired (not logged in -> the run says login_required),
CloudflareBlocked (403/429/challenge), HttpError (one search failed / request budget used up).
No kashrut data on the site: like Shufersal, only the product name is read (בד"ץ / מהדרין), otherwise
'כשרות לא ידועה'. Cart writes are NOT here: they go through Command Center's approval flow only.
"""
from __future__ import annotations

import html
import re
from typing import Callable, Dict, List, Optional

from .. import kashrut as K
from .. import normalize as N
from ..attrs import extract_attrs
from ..http import HttpError, SessionExpired
from ..model import Product
from .base import SiteAdapter

ORIGIN = "https://super-hist.co.il"
CART_PAGE = ORIGIN + "/index.php?route=checkout/cart"
MIN_INTERVAL = 10.0          # enforced by command_center.super_hist._throttle; used for runtime estimates
DEFAULT_BUDGET = 40          # max searches per run (each is >=10 s); beyond -> HttpError -> item goes elsewhere

_transport: List[Optional[Callable[[str], List[dict]]]] = [None]


def set_transport(fn: Optional[Callable[[str], List[dict]]]) -> None:
    """Command Center wires its logged-in, throttled search here (None = not configured)."""
    _transport[0] = fn


def _money(value) -> Optional[float]:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value).replace("\u200f", "").replace("\u200e", ""))
    return round(float(m.group(0).replace(",", "")), 2) if m else None


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


_NOT_KOSHER = re.compile(r"לא\s*כשר")


def _kashrut(name: str) -> Optional[List[str]]:
    """Name-only, like Shufersal. 'לא כשר' in the name is KNOWN not kosher (kosher items then go elsewhere);
    nothing said -> None ('כשרות לא ידועה')."""
    if _NOT_KOSHER.search(name or ""):
        return ["לא כשר (לפי שם המוצר)"]
    return K.name_hechsherim(name)


class SuperHistSearcher:
    """Live searcher over the injected transport, with a per-run request budget."""

    def __init__(self, transport: Optional[Callable[[str], List[dict]]] = None, budget: Optional[int] = None):
        self.transport = transport or _transport[0]
        self.budget = DEFAULT_BUDGET if budget is None else budget
        self.request_count = 0

    def search(self, term: str):
        if self.transport is None:
            raise SessionExpired("הסופר החברתי: אין חיבור מוגדר (Command Center לא חיבר את החיפוש)")
        if self.request_count >= self.budget:
            raise HttpError(f"תקציב החיפושים בסופר החברתי ({self.budget}) נוצל — לא חיפשתי")
        self.request_count += 1
        return list(self.transport(term) or [])


class SuperHistAdapter(SiteAdapter):
    name = "super_hist"
    display_name = "הסופר החברתי"
    block_msg = "הסופר החברתי חסם/ביקש אימות — עצרתי בלי לנסות שוב"
    fixture_results_key = "data"
    robots = None
    crawl_delay = MIN_INTERVAL
    deep_size = None             # the site returns its own top results; no page-size parameter
    max_extra_searches = 1       # each search costs >=10 s

    def __init__(self, searcher=None, store: Optional[str] = None):
        super().__init__(searcher, None)

    def make_live_searcher(self, client=None, size: int = 10):
        return SuperHistSearcher()

    def normalize(self, raw: dict, expected_unit: Optional[str] = None) -> Optional[Product]:
        if not isinstance(raw, dict) or not str(raw.get("product_id", "")).isdigit():
            return None
        name = " ".join(html.unescape(str(raw.get("name") or "")).split())
        shelf = _money(raw.get("price"))
        special = _money(raw.get("special"))
        price = special if special is not None else shelf          # always the sale price when there is one
        try:
            minimum = float(str(raw.get("minimum") or 1))
        except ValueError:
            minimum = 1.0
        by_weight = (bool(str(raw.get("weight_unit_price_text") or "").strip()) or _truthy(raw.get("weight"))
                     or not minimum.is_integer())
        pack = N.compute_pack({"name": name, "prop": {"by_kilo": 1 if by_weight else 0}}, expected_unit)
        ribbon = raw.get("bgfnribbon_text") if isinstance(raw.get("bgfnribbon_text"), str) else ""
        notes = [x for x in (ribbon.strip(), "מחיר מבצע" if special is not None and shelf and special < shelf
                             else "") if x]
        href = str(raw.get("href") or "")
        return Product(site=self.name, id=str(raw["product_id"]), name=name, brand="",
                       price=price, shelf_price=shelf,
                       promo_used=("מחיר מבצע" if special is not None and shelf and special < shelf else None),
                       promo_note="; ".join(notes), pack=pack, unit_price=N.unit_price(price, pack),
                       available=not _truthy(raw.get("out_of_stock")),
                       cats={"departments": None, "groups": None, "sub_groups": None},
                       step=None, sell_method="weight" if by_weight else "unit",
                       link_code=str(raw["product_id"]),
                       attrs=extract_attrs(name, ""),
                       kashrut=_kashrut(name), kashrut_levels=K.name_level(name),
                       raw=dict(raw, _url=href if href.startswith(ORIGIN + "/") else None,
                                _minimum=int(minimum) if minimum.is_integer() and minimum >= 1 else 1))

    # ---------------- cart: only via Command Center approvals (never from this package) ----------------
    @property
    def checkout_url(self) -> str:
        return CART_PAGE          # the cart PAGE (Ben reviews and pays himself); never the checkout

    @property
    def default_session_path(self) -> str:
        return ""

    def load_session(self, path: str = ""):
        raise NotImplementedError("Super-Hist session lives in Command Center (super_hist_login_*)")

    def cart_get(self, session, client) -> Dict[str, float]:
        raise NotImplementedError("Super-Hist cart: command_center.super_hist (approval flow) only")

    def cart_merge_post(self, session, client, ours, policy="max", plan_store=None, log=print) -> dict:
        raise NotImplementedError("Super-Hist cart writes: command_center.super_hist approvals only")
