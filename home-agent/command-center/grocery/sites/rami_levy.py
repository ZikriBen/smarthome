"""Rami Levy Online adapter.

Search  GET https://www.rami-levy.co.il/api/search?q=&store=&size=   (public, verified 2026-10-19;
        size=40 verified live on the NUC 2026-10-19 — store 331 then still shows only the 12-pack of eggs)
Cart    PHASE 2, DISABLED by default. GET customer state (current cart) -> merge ->
        POST /api/v2/cart (REPLACE semantics: body = whole cart) -> diff -> checkout link.
        Never checks out, never pays.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import stat
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from .. import normalize as N
from ..cartlogic import CartError, diff, merge, to_qty
from ..http import CHROME_UA, Client, CLOUDFLARE_MSG, HttpError
from ..model import Product
from .base import SiteAdapter

WEB_ORIGIN = "https://www.rami-levy.co.il"
API_ORIGIN = "https://www-api.rami-levy.co.il"
SEARCH_URL = WEB_ORIGIN + "/api/search"
CART_URL = WEB_ORIGIN + "/api/v2/cart"
CUSTOMER_URL = API_ORIGIN + "/api/v2/site/clubs/customer/{uid}"
CHECKOUT_URL = "https://www.rami-levy.co.il/he/dashboard/checkout"
DEFAULT_SESSION_PATH = "/data/rami-levy-session.json"
FEE_IDS = ("164854",)            # server-added delivery-fee line ("מחיר משלוח")


@dataclass
class RamiSession:
    ecomtoken: str
    bearer: Optional[str] = None
    cookie: Optional[str] = None
    user_agent: str = CHROME_UA

    def __repr__(self):  # never leak secrets via repr/logging
        return "RamiSession(ecomtoken=[redacted], bearer=%s, cookie=%s, user_agent=%r)" % (
            "[redacted]" if self.bearer else None, "[redacted]" if self.cookie else None, self.user_agent)

    __str__ = __repr__

    def claims(self) -> dict:
        try:
            payload = self.ecomtoken.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            raise CartError("ecomtoken is not a decodable JWT")

    @property
    def uid(self) -> str:
        uid = self.claims().get("id")
        if uid in (None, ""):
            raise CartError("ecomtoken has no 'id' claim")
        return str(uid)

    def days_left(self, now: Optional[dt.datetime] = None) -> Optional[float]:
        exp = self.claims().get("exp")
        if not exp:
            return None
        now = now or dt.datetime.now(dt.timezone.utc)
        return (float(exp) - now.timestamp()) / 86400.0

    def headers(self) -> Dict[str, str]:
        h = {"ecomtoken": self.ecomtoken, "accept": "application/json, text/plain, */*",
             "content-type": "application/json;charset=UTF-8", "locale": "he",
             "origin": WEB_ORIGIN, "referer": WEB_ORIGIN + "/", "user-agent": self.user_agent or CHROME_UA}
        if self.bearer:
            h["authorization"] = self.bearer if self.bearer.lower().startswith("bearer ") else f"Bearer {self.bearer}"
        if self.cookie:
            h["cookie"] = self.cookie
        return h


def check_session_file(path: str, warn: Callable[[str], None] = print) -> dict:
    if not os.path.exists(path):
        raise CartError(f"session file not found: {path}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        warn(f"warning: {path} is readable by group/others (mode {oct(mode)}); run chmod 600")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_store(customer: dict) -> Optional[str]:
    dtc = customer.get("deliveryTimesCustomer") or {}
    store = dtc.get("store_id") if isinstance(dtc, dict) else None
    if store in (None, ""):
        store = (customer.get("user") or {}).get("store_id")
    return str(store) if store not in (None, "") else None


def parse_current_cart(customer: dict) -> Dict[str, float]:
    """Customer-state `cart` -> {id: qty}. Shape NOT verified by us; accepts the variants in prior art:
    list of {C, Quantity} (bornio), list of {id, quantity}, or a dict {id: qty}."""
    cart = customer.get("cart")
    if not cart:
        return {}
    items = cart.get("items") if isinstance(cart, dict) else cart
    out: Dict[str, float] = {}
    if isinstance(items, dict):
        for k, v in items.items():
            if str(k) not in FEE_IDS and to_qty(v) > 0:
                out[str(k)] = to_qty(v)
    elif isinstance(items, list):
        for e in items:
            if not isinstance(e, dict):
                continue
            pid = e.get("C", e.get("id"))
            q = e.get("Quantity", e.get("quantity", e.get("qty")))
            if pid not in (None, "") and str(pid) not in FEE_IDS and to_qty(q) > 0:
                out[str(pid)] = to_qty(q)
    return out


def returned_items(response: dict) -> Dict[str, float]:
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise CartError(f"unexpected cart response shape: {str(response)[:200]}")
    return {str(e["id"]): to_qty(e.get("quantity", e.get("Quantity", 0)))
            for e in items if isinstance(e, dict) and e.get("id") not in (None, "")}


def supply_at(customer_cart: Optional[dict], days: int = 2, now: Optional[dt.datetime] = None) -> str:
    if isinstance(customer_cart, dict) and customer_cart.get("supplyAt"):
        return str(customer_cart["supplyAt"])
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now + dt.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000Z")


class RamiLevySearcher:
    def __init__(self, client: Client, store: str, size: int = 10, user_agent: str = CHROME_UA):
        self.client, self.store, self.size, self.ua = client, str(store), size, user_agent

    def search(self, term: str, size: Optional[int] = None):
        qs = urllib.parse.urlencode({"q": term, "store": self.store, "size": size or self.size})
        headers = {"user-agent": self.ua, "accept": "application/json, text/plain, */*",
                   "accept-language": "he-IL,he;q=0.9,en;q=0.8", "locale": "he", "referer": WEB_ORIGIN + "/"}
        data = self.client.request_json("GET", f"{SEARCH_URL}?{qs}", headers)
        return (data or {}).get("data") or []


class RamiLevyAdapter(SiteAdapter):
    name = "rami_levy"
    display_name = "רמי לוי"
    system_line_ids = FEE_IDS
    block_msg = CLOUDFLARE_MSG
    fixture_results_key = "data"

    def __init__(self, searcher=None, store: Optional[str] = "331"):
        super().__init__(searcher, str(store or "331"))

    def make_live_searcher(self, client, size: int = 10):
        return RamiLevySearcher(client, self.store, size)

    def normalize(self, raw: dict, expected_unit: Optional[str] = None) -> Optional[Product]:
        if not isinstance(raw, dict) or raw.get("id") in (None, ""):
            return None
        pack = N.compute_pack(raw, expected_unit)
        eff, promo = N.effective_price(raw)
        try:
            step = float(raw.get("multiplication")) if raw.get("multiplication") not in (None, "") else None
        except (TypeError, ValueError):
            step = None
        gs = raw.get("gs") if isinstance(raw.get("gs"), dict) else {}
        cats = {k: (str(raw[f]) if raw.get(f) not in (None, "") else None)
                for k, f in (("departments", "department_id"), ("groups", "group_id"), ("sub_groups", "sub_group_id"))}
        return Product(
            site=self.name, id=str(raw["id"]), name=str(raw.get("name") or "").strip(),
            brand=str(gs.get("BrandName") or ""), price=eff, shelf_price=N.shelf_price(raw),
            promo_used=promo.label if promo else None, promo_note=N.promo_note(raw), pack=pack,
            unit_price=N.unit_price(eff, pack), available=N.available_in_store(raw, self.store),
            cats=cats, step=step, sell_method="weight" if pack.by_weight else "unit",
            link_code=(str(raw["barcode"]).strip() or None) if raw.get("barcode") not in (None, "") else None,
            raw=raw)

    # ---------------- phase 2 ----------------
    @property
    def checkout_url(self) -> str:
        return CHECKOUT_URL

    @property
    def default_session_path(self) -> str:
        return DEFAULT_SESSION_PATH

    def load_session(self, path: str = DEFAULT_SESSION_PATH, warn=print) -> RamiSession:
        raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")

    def _customer(self, session: RamiSession, client: Client) -> dict:
        raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")

    def cart_get(self, session: RamiSession, client: Client) -> Dict[str, float]:
        raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")

    def cart_merge_post(self, session: RamiSession, client: Client, ours: Dict[str, float],
                        policy: str = "max", plan_store: Optional[str] = None, log=print) -> dict:
        raise NotImplementedError("cart/account access is not vendored into Command Center (links only)")
