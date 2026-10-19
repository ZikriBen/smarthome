"""Shufersal Online adapter (shufersal.co.il, SAP Hybris "online/he").

Endpoints (from prior art: eshaham/shufersal-automation, matipojo/shufersal-mcp; NOT verified live by us,
because the box gets a CloudFront error/"Maintenance" page on every URL — see README):
  Search   GET  /online/he/search/results?q=<term>&limit=<n>   (accept: application/json,
                x-requested-with: XMLHttpRequest)  -> {results:[...], pagination:{...}}
           eshaham appends ":relevance" to q; matipojo doesn't. We send the plain term.
  Product  GET  /online/he/products/<P_code>
  Cart     GET  /online/he/cart/load?restoreCart=true        -> HTML, <article data-product-code data-entry-qty ...>
           POST /online/he/cart/addGrid   [{productCode, quantity, frontQuantity, sellingMethod, longTail:false}]
                header csrftoken: <ACC.config.CSRFToken from any page>, plus the logged-in cookie.
           POST /online/he/cart/update?entryNumber=&qty=      (not used: we only add, never remove)
  Checkout /online/he/cart/cartsummary   (Ben opens it himself; we NEVER create orders)
Login: username+password form (#j_username/#j_password) — we never automate it; Ben captures the cookie.
"""
from __future__ import annotations

import re
import urllib.parse
from html.parser import HTMLParser
from typing import Dict, Optional

from .. import normalize as N
from ..cartlogic import CartError, diff, merge, to_qty
from ..http import CHROME_UA, Client, HttpError, Response, RobotsPolicy
from ..model import Product
from ..attrs import extract_attrs
from .base import SiteAdapter
from .rami_levy import check_session_file

ORIGIN = "https://www.shufersal.co.il"
WEBAPP = ORIGIN + "/online/he"
SEARCH_URL = WEBAPP + "/search/results"
CART_LOAD_URL = WEBAPP + "/cart/load?restoreCart=true"
CART_ADD_URL = WEBAPP + "/cart/addGrid"
CHECKOUT_URL = WEBAPP + "/cart/cartsummary"
DEFAULT_SESSION_PATH = "/data/shufersal-session.json"
BLOCK_MSG = ("Shufersal returned its CloudFront error / 'Maintenance' page or a bot-protection page — "
             "blocked from this IP (or real maintenance). Run from an Israeli home IP (the NUC); "
             "this tool does not try to bypass it.")

# robots.txt as captured from the NUC (Israeli home IP) on 2026-10-19 11:22 IL, runs/20261019-112140-nuc/
# captures/sh-robots.txt (the box only gets the CloudFront Maintenance page). `User-agent: *` group only.
# Search (/online/he/search/results?q=...) is ALLOWED. /online/he/cart* and /online/he/checkout are DISALLOWED for
# crawlers; Ben decided (2026-10-19) that phase 2 may use /online/he/cart* on HIS OWN logged-in session (he still
# checks out himself; we never touch /checkout). Phase-2 clients therefore use robots_mode="note" (logged, not
# enforced), with the same 10 s Crawl-delay and the stop-on-403/429/challenge rules.
ROBOTS_TXT = """User-agent: *
Disallow: /online/he/cart
Disallow: /online/he/checkout
Disallow: /online/he/my-account
Disallow: *?query=*
Disallow: *online/he/login?redirect_url=*
Disallow: *shuf_source=*
Disallow: */marketplace/
Request-rate: 1/10
Crawl-delay: 10
Visit-time: 0400-0845
"""
ROBOTS = RobotsPolicy(ROBOTS_TXT, source="shufersal.co.il/robots.txt captured 2026-10-19")
CRAWL_DELAY = 10.0
_CSRF_RE = re.compile(r"CSRFToken\s*[:=]\s*['\"]([^'\"]+)['\"]")

_UNCOND_BAD = ("מועדון", "כרטיס", "אשראי", "you", "דיל", "קופון", "אישי")


def is_block_page(resp: Response) -> bool:
    t = resp.text[:3000]
    return ("errorpage/Maintenance" in t or "Error from cloudfront" in resp.header("x-cache")
            or "cf-mitigated" in {k.lower() for k in resp.headers} or "Attention Required" in t)


class ShufersalSession:
    def __init__(self, cookie: str, csrf_token: Optional[str] = None, user_agent: str = CHROME_UA):
        self.cookie, self.csrf_token, self.user_agent = cookie, csrf_token, user_agent or CHROME_UA

    def __repr__(self):
        return f"ShufersalSession(cookie=[redacted], csrf_token={'[redacted]' if self.csrf_token else None})"

    __str__ = __repr__

    def headers(self, json_body=False) -> Dict[str, str]:
        h = {"user-agent": self.user_agent, "accept": "application/json, text/plain, */*",
             "x-requested-with": "XMLHttpRequest", "referer": WEBAPP + "/", "cookie": self.cookie}
        if json_body:
            h["content-type"] = "application/json"
            if self.csrf_token:
                h["csrftoken"] = self.csrf_token
        return h


class _CartHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items: Dict[str, float] = {}
        self.entries: Dict[str, int] = {}
        self.login_form = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "article" and a.get("data-product-code"):
            code = a["data-product-code"]
            self.items[code] = self.items.get(code, 0.0) + to_qty(a.get("data-entry-qty") or 0)
            try:
                self.entries[code] = int(a.get("data-entry-number") or 0)
            except ValueError:
                pass
        if tag == "input" and a.get("id") == "j_username":
            self.login_form = True


def parse_cart_html(html: str) -> Dict[str, float]:
    p = _CartHTML()
    p.feed(html or "")
    if p.login_form:
        raise CartError("Shufersal returned the login page — session cookie expired; re-login in a browser")
    return {k: v for k, v in p.items.items() if v > 0}


def _price_val(p) -> Optional[float]:
    if isinstance(p, dict):
        p = p.get("value")
    try:
        return float(p) if p not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _code(o) -> Optional[str]:
    if isinstance(o, dict):
        return str(o.get("code")) if o.get("code") not in (None, "") else None
    return str(o) if o not in (None, "") else None


class ShufersalSearcher:
    def __init__(self, client: Client, size: int = 10, user_agent: str = CHROME_UA):
        self.client, self.size, self.ua = client, size, user_agent

    def search(self, term: str, size: Optional[int] = None):
        qs = urllib.parse.urlencode({"q": term, "limit": size or self.size})
        headers = {"user-agent": self.ua, "accept": "application/json", "x-requested-with": "XMLHttpRequest",
                   "accept-language": "he-IL,he;q=0.9,en;q=0.8", "referer": WEBAPP + "/"}
        data = self.client.request_json("GET", f"{SEARCH_URL}?{qs}", headers)
        return (data or {}).get("results") or []


class ShufersalAdapter(SiteAdapter):
    name = "shufersal"
    display_name = "שופרסל"
    block_msg = BLOCK_MSG
    fixture_results_key = "results"
    robots = ROBOTS
    crawl_delay = CRAWL_DELAY     # robots.txt Crawl-delay / Request-rate 1/10

    def __init__(self, searcher=None, store: Optional[str] = None):
        # Shufersal has no store param: assortment/stock follow the session's delivery area.
        super().__init__(searcher, None)

    def make_live_searcher(self, client, size: int = 10):
        return ShufersalSearcher(client, size)

    def normalize(self, raw: dict, expected_unit: Optional[str] = None) -> Optional[Product]:
        if not isinstance(raw, dict) or not raw.get("code"):
            return None
        name = str(raw.get("name") or "").strip()
        by_weight = ((raw.get("sellingMethod") or {}).get("code") == "BY_WEIGHT")
        shelf = _price_val(raw.get("price"))
        eff_raw = raw.get("effectivePrice")
        try:
            eff_raw = float(eff_raw) if eff_raw not in (None, "") else None
        except (TypeError, ValueError):
            eff_raw = None
        msg = str(raw.get("promotionMsg") or "").strip()
        promo_used = None
        price = shelf if shelf is not None else eff_raw
        if eff_raw is not None and shelf is not None and eff_raw < shelf:
            low = msg.lower()
            if not N._MULTIBUY_RE.search(msg) and not any(w in low for w in _UNCOND_BAD):
                price, promo_used = eff_raw, msg or "מחיר מבצע"
        # pack: name + unitDescription ("1 ליטר", "500 גרם") through the shared normalizer
        pseudo = {"name": f"{name} {raw.get('unitDescription') or ''}".strip(),
                  "prop": {"by_kilo": 1 if by_weight else 0}}
        pack = N.compute_pack(pseudo, expected_unit)
        up = N.unit_price(price, pack)
        if up is None:
            # fall back to the site's own comparison price: pricePerUnit / unitForComparison ("100 גרם")
            ppu = _price_val(raw.get("pricePerUnit"))
            sizes = N.parse_name_sizes(str(raw.get("unitForComparison") or ""))
            u = N.norm_uom(raw.get("unitForComparison"))
            if ppu and (sizes or u):
                if sizes:
                    unit, amount = sizes[0][0], sizes[0][1]
                else:
                    # real data: unitForComparison is just 'מ"ל'/'גרם' and pricePerUnit is per 100 of it
                    # (milk 1 L ₪7.35 -> pricePerUnit 0.74). Counts/kg/L are per 1.
                    unit, amount = u[0], u[1] * (100 if u[1] < 1 else 1)
                if amount:
                    up = round(ppu / amount, 4)
                    pack = N.Pack(unit, None, source="site_unit_price", note="מחיר ליחידה מהאתר")
        stock = ((raw.get("stock") or {}).get("stockLevelStatus") or {}).get("code")
        available = (stock in (None, "inStock")) and raw.get("purchasable") is not False
        cats = {"departments": _code(raw.get("commercialDepartment")),
                "groups": _code(raw.get("commercialCategoryGroup")),
                "sub_groups": _code(raw.get("commercialCategorySubGroup"))}
        notes = [msg] if msg else []
        notes += [str(x) for x in (raw.get("promotions") or []) if isinstance(x, str) and x not in notes]
        brand = (raw.get("brand") or {}).get("name") if isinstance(raw.get("brand"), dict) else raw.get("brand")
        return Product(site=self.name, id=str(raw["code"]), name=name, brand=str(brand or ""),
                       price=price, shelf_price=shelf, promo_used=promo_used, promo_note="; ".join(notes),
                       pack=pack, unit_price=up, available=available, cats=cats,
                       step=float(raw.get("minOrderWeight")) if by_weight and raw.get("minOrderWeight") else None,
                       sell_method="weight" if by_weight else "unit", link_code=str(raw["code"]),
                       attrs=extract_attrs(name, str(brand or "")),
                       raw=raw)

    # ---------------- phase 2 ----------------
    @property
    def checkout_url(self) -> str:
        return CHECKOUT_URL

    @property
    def default_session_path(self) -> str:
        return DEFAULT_SESSION_PATH

    def load_session(self, path: str = DEFAULT_SESSION_PATH, warn=print) -> ShufersalSession:
        raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")

    def _csrf(self, session: ShufersalSession, client: Client, html: str = "") -> str:
        """Session file token, else the token in a page we already fetched (the cart page), else one GET of
        the home page. Never logged."""
        raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")

    def _cart_html(self, session: ShufersalSession, client: Client) -> str:
        raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")

    def cart_get(self, session: ShufersalSession, client: Client) -> Dict[str, float]:
        raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")

    def cart_merge_post(self, session: ShufersalSession, client: Client, ours: Dict[str, float],
                        policy: str = "max", plan_store: Optional[str] = None, log=print,
                        sell_methods: Optional[Dict[str, str]] = None) -> dict:
        """Shufersal's cart is ADD-based (not replace), so we only send the positive delta per product:
        target = merge(current, ours, policy); add = target - current. Nothing is ever removed/decreased.
        Requests (each >= 10 s apart): GET /cart/load (current cart; CSRF token taken from it if present)
        [+ GET /online/he/ only if no token in the session file or the cart page] -> ONE POST /cart/addGrid with all
        lines -> GET /cart/load to verify. Nothing to add -> only the first GET. Never /checkout."""
        raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")
