"""הסופר החברתי (super-hist.co.il) deals scan — READ-ONLY. "Don't miss the promotions on what Ben buys."

Source = the site's promotion CATEGORIES, not per-item searches (Ben, 2026-10-21):
  * /משלמים-פחות-מקבלים-יותר/  (SUPER_HIST_DEALS_PROMO_PAGES=1; OFF until its markup is verified live, then on)
  * /יוקר-המחייה/               (SUPER_HIST_DEALS_COST_OF_LIVING=1; candidate second category, off by default)
  Page 1, then ?page=N only while the page itself links to the next page, up to SUPER_HIST_DEALS_MAX_PAGES (5).
Plus Ben's cart page (2-3 requests: account check, cart, mini-cart canary only when it reads empty), read live
because it is cheap; a cart line showing <del>shelf</del><ins>sale</ins> is a deal by itself.
Everything goes through super_hist's transport: the URL allowlist (GET of the two categories, page 1 / ?page=N),
>= MIN_INTERVAL (10 s) between requests, the 24 h stop on 403/429/captcha (also persisted in grocery_site_state),
and at most ONE automatic login per run when logged out (the existing SMS flow, code via the Telegram relay,
bounded to 3 minutes; failure/timeout -> one line and stop; never a resend or a retry).

Local matching (no extra requests): each category product is matched against the cart (product id), then the
CACHED grocery list (state/current_list.json — no Google Tasks sync from here), regulars (items in >= 2 past
cart runs) and staples (defaults.yaml), using the items' own include/exclude words. Matched -> "על מה שאתה
קונה" (list > cart > regulars > staples, by % off); the rest -> up to NOTABLE_MAX food deals at
>= NOTABLE_MIN_PCT off. Non-food is skipped. Kashrut: not in the report (SUPER_HIST_DEALS_KASHRUT=1 turns the
name-based 🔍 flag back on). Dates: shown only when the ribbon text has one ("תוקף לא מוצג" otherwise).
Nothing here writes to the site, the cart or the grocery list.

Entry points: run(...) (pure, injectable), start()/status() (async job for the API/MCP), last() (the previous
report, no site traffic) and `python -m command_center.super_hist_deals` (CLI; see main()).
"""

import datetime as dt
import json
import os
import re
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

from . import super_hist as sh

NOTABLE_MIN_PCT = 25          # "other notable deals": food at >= this % off
NOTABLE_MAX = 6
MINE_MAX = 25
STATUS_WAIT_MAX = 90
STALE_SECONDS = 20 * 60
HARD_MAX_PAGES = 10
MAX_PAGES = max(1, min(int(os.environ.get("SUPER_HIST_DEALS_MAX_PAGES", "5") or 5), HARD_MAX_PAGES))
KASHRUT_ENABLED = os.environ.get("SUPER_HIST_DEALS_KASHRUT", "0") == "1"
PERSONAL_DEFAULT = os.environ.get("SUPER_HIST_DEALS_PERSONAL", "0") == "1"   # match list/cart/regulars
TOP_DEALS = max(1, int(os.environ.get("SUPER_HIST_DEALS_TOP", "15") or 15))
PROMO_PATH = "/משלמים-פחות-מקבלים-יותר/"
COST_OF_LIVING_PATH = "/יוקר-המחייה/"
CATEGORY_PATHS = {"promo": PROMO_PATH, "cost_of_living": COST_OF_LIVING_PATH}
CATEGORY_URLS = {k: sh.BASE + urllib.parse.quote(v) for k, v in CATEGORY_PATHS.items()}
CATEGORY_HE = {"promo": "משלמים פחות מקבלים יותר", "cost_of_living": "מתמודדים עם יוקר המחיה"}
CATEGORY_FLAGS = {"promo": "SUPER_HIST_DEALS_PROMO_PAGES", "cost_of_living": "SUPER_HIST_DEALS_COST_OF_LIVING"}
PROMO_URL = CATEGORY_URLS["promo"]


def enabled_categories():
    return [k for k in CATEGORY_PATHS if os.environ.get(CATEGORY_FLAGS[k], "0") == "1"]


OUT_DIR = Path(os.environ.get("SUPER_HIST_DEALS_DIR", "/workspace/grocery/super_hist_deals"))
SITE = "super_hist"

TIER_LIST, TIER_CART, TIER_REGULAR, TIER_STAPLE, TIER_NOTABLE, TIER_ALL = 1, 2, 3, 4, 9, 10
TIER_HE = {TIER_LIST: "ברשימה", TIER_CART: "בעגלה", TIER_REGULAR: "קונה בקביעות", TIER_STAPLE: "מוצר קבוע"}

_runs = {}
_runs_lock = threading.Lock()
_job_lock = threading.Lock()


# =========================================================================== promo text (pure)

_NUM = r"(\d+(?:[.,]\d{1,2})?)"
_MULTI = [re.compile(r"(\d{1,2})\s*(?:יח['׳]?\s*)?(?:ב|ב-|ב־|ב\s*-|ב\s*ב?)\s*₪?\s*" + _NUM + r"\s*₪?"),   # 2 ב-10 / 3 ב 20₪
          re.compile(r"(\d{1,2})\s*(?:יח['׳]?\s*)?(?:ב|ב-)?\s*₪\s*" + _NUM)]                               # 2 ₪10
_ONE_PLUS = re.compile(r"(\d)\s*\+\s*(\d)")                                                        # 1+1
_PRICE_ONLY = re.compile(r"^\s*(?:מחיר\s*מבצע\s*)?(?:₪\s*" + _NUM + r"|" + _NUM + r"\s*₪)\s*$")
_SECOND_PCT = re.compile(r"(?:השני|השנייה|השניה|ה-?2)\s*ב-?\s*(\d{1,2})\s*%")                       # השני ב-50%
_PCT = re.compile(r"(\d{1,2})\s*%\s*(?:הנחה)?")
_DATE_UNTIL = re.compile(r"(?:עד|תוקף)\s*(?:ה-?|ל-?)?\s*(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d])")
_DATE_FULL = re.compile(r"(?<![\d.])(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?![\d])")


def _f(text):
    return float(str(text).replace(",", "."))


def parse_promo(text, shelf=None, today=None):
    """Ribbon text -> {"terms", "n", "total", "unit", "pct", "until"} (all optional). Unknown formats keep the
    text as terms with no computed price. ASSUMED formats (no live ribbon text seen yet)."""
    text = " ".join(str(text or "").split())
    out = {"terms": text or None, "n": None, "total": None, "unit": None, "pct": None, "until": None}
    if not text:
        return out
    m = _DATE_UNTIL.search(text) or _DATE_FULL.search(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = m.group(3)
        today = today or dt.date.today()
        year = (int(year) + 2000 if year and len(year) == 2 else int(year)) if year else today.year
        try:
            until = dt.date(year, month, day)
            if not m.group(3) and until < today - dt.timedelta(days=180):
                until = dt.date(year + 1, month, day)
            out["until"] = until.isoformat()
        except ValueError:
            pass
        text_wo_date = text[:m.start()] + text[m.end():]
    else:
        text_wo_date = text
    plus = _ONE_PLUS.search(text_wo_date)
    second = _SECOND_PCT.search(text_wo_date)
    multi = next((m for rx in _MULTI for m in [rx.search(text_wo_date)] if m), None)
    if plus:
        buy, free = int(plus.group(1)), int(plus.group(2))
        if buy >= 1 and free >= 1:
            out["n"] = buy + free
            out["pct"] = round(100.0 * free / (buy + free))
            if shelf:
                out["unit"] = round(shelf * buy / (buy + free), 2)
    elif second:
        off = int(second.group(1))
        out["n"] = 2
        out["pct"] = round(off / 2.0)
        if shelf:
            out["unit"] = round(shelf * (1 - off / 200.0), 2)
    elif multi and int(multi.group(1)) >= 2:
        n, total = int(multi.group(1)), _f(multi.group(2))
        if total > 0:
            out.update(n=n, total=round(total, 2), unit=round(total / n, 2))
            if shelf and shelf > out["unit"]:
                out["pct"] = round(100.0 * (shelf - out["unit"]) / shelf)
    else:
        pct = _PCT.search(text_wo_date)
        price = _PRICE_ONLY.search(text_wo_date)
        if pct:
            out["pct"] = int(pct.group(1))
            if shelf:
                out["unit"] = round(shelf * (1 - out["pct"] / 100.0), 2)
        elif price and shelf and 0 < _f(price.group(1) or price.group(2)) < shelf:   # "₪36.90" = the promo price
            out["unit"] = round(_f(price.group(1) or price.group(2)), 2)
            out["pct"] = round(100.0 * (shelf - out["unit"]) / shelf)
    return out


def deal_of(product, today=None):
    """A normalized product (super_hist.parse_search shape) -> deal dict, or None when it is not on a deal.
    Effective unit price = the better of the sale price and the ribbon's per-unit price."""
    shelf, price = product.get("shelf_price"), product.get("price")
    promo = parse_promo(product.get("promo"), shelf, today) if product.get("promo") else None
    sale = price if (price is not None and shelf is not None and price < shelf) else None
    if sale is None and not promo:
        return None
    candidates = []
    if sale is not None:
        candidates.append(("sale", sale, round(100.0 * (shelf - sale) / shelf) if shelf else None))
    if promo and promo.get("unit") is not None:
        candidates.append(("promo", promo["unit"], promo.get("pct")))
    best = min(candidates, key=lambda c: c[1]) if candidates else None
    pct = best[2] if best else (promo or {}).get("pct")
    return {"product_id": str(product.get("product_id") or ""), "name": product.get("name") or "",
            "shelf_price": shelf, "sale_price": sale, "effective_unit": best[1] if best else None,
            "pct_off": pct, "terms": (promo or {}).get("terms"), "multi_n": (promo or {}).get("n"),
            "multi_total": (promo or {}).get("total"), "until": (promo or {}).get("until"),
            "url": product.get("url") or product_link(product.get("product_id")),
            "out_of_stock": bool(product.get("out_of_stock")), "in_cart": bool(product.get("in_cart")),
            "weighted": bool(product.get("weighted"))}


def product_link(product_id):
    """Standard OpenCart product URL (the search JSON's href has this exact shape)."""
    pid = str(product_id or "")
    return f"{sh.BASE}/index.php?route=product/product&product_id={pid}" if pid.isdigit() else None


# =========================================================================== promo category page (pure, ASSUMED)

class _CategoryParser(sh.html.parser.HTMLParser):
    """ASSUMED istores/OpenCart grid markup (not seen live): a card element with class product-thumb /
    product-layout / product-item carrying data-product_id (or a link with product_id=); name = the card's
    first link text inside .name/.caption/.product-description (else the first link text); price = ins/del
    inside .price (or .price-new/.price-old); ribbon = .ribbonbgfn / .product-ribbonbgfn text."""

    CARD = {"product-thumb", "product-layout", "product-item"}
    VOID = sh._CartParser.VOID

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.cards, self.card = [], [], None
        self.page_links = set()

    def _in(self, *classes):
        return any(c in cls for _t, cls, _m in self.stack for c in classes)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        mark = None
        if self.card is None and classes & self.CARD:
            self.card = {"product_id": a.get("data-product_id") or "", "href": "", "name": "", "any_name": "",
                         "ins": "", "del": "", "price": "", "new": "", "old": "", "ribbon": ""}
            mark = "card"
        if tag == "a":
            m_page = re.search(r"[?&]page=(\d{1,3})(?:&|$)", a.get("href") or "")
            if m_page:
                self.page_links.add(int(m_page.group(1)))
        if self.card is not None and tag == "a":
            href = a.get("href") or ""
            m = re.search(r"product_id=(\d+)", href)
            if m and not self.card["product_id"]:
                self.card["product_id"] = m.group(1)
            if not self.card["href"] and (m or href.startswith(sh.BASE)):
                self.card["href"] = href
        if tag in self.VOID:
            return
        self.stack.append((tag, classes, mark))

    def handle_endtag(self, tag):
        if tag in self.VOID or not any(t == tag for t, _c, _m in self.stack):
            return
        while self.stack:
            t, _c, mark = self.stack.pop()
            if mark == "card" and self.card is not None:
                self.cards.append(self.card)
                self.card = None
            if t == tag:
                break

    def handle_data(self, data):
        if self.card is None or not data.strip():
            return
        top = self.stack[-1][0] if self.stack else ""
        if self._in("ribbonbgfn", "product-ribbonbgfn"):
            self.card["ribbon"] += data
        elif self._in("price", "price_span", "product-price"):
            key = "ins" if top == "ins" else "del" if top == "del" else (
                "new" if self._in("price-new", "special-price") else "old" if self._in("price-old") else "price")
            self.card[key] += data
        elif top == "a":
            if self._in("name", "caption", "product-description", "product-name"):
                self.card["name"] += data
            elif not self.card["any_name"]:
                self.card["any_name"] += data


def parse_category_page(page, current=1):
    """Category HTML -> (products in the parse_search shape, has_next). has_next = the page links to
    ?page=current+1. Raises SiteChanged when page 1 has no product cards (markup differs from the assumption:
    the scan skips that category with a warning)."""
    parser = _CategoryParser()
    parser.feed(page or "")
    parser.close()
    out = []
    for c in parser.cards:
        pid = str(c["product_id"] or "")
        if not pid.isdigit():
            continue
        sale = sh._money(c["ins"]) or sh._money(c["new"])
        shelf = sh._money(c["del"]) or sh._money(c["old"])
        plain = sh._money(c["price"])
        shelf = shelf if shelf is not None else plain
        out.append({"product_id": pid, "name": sh._clean(sh.html.unescape(c["name"] or c["any_name"]), 120),
                    "price": sale if sale is not None else shelf, "shelf_price": shelf,
                    "on_sale": sale is not None and shelf is not None and sale < shelf,
                    "promo": sh._clean(c["ribbon"], 60) or None, "in_cart": False, "out_of_stock": False,
                    "minimum": 1, "weighted": False, "url": sh._product_url(c["href"]) or product_link(pid)})
    if not out and int(current) == 1:
        raise sh.SiteChanged("דף המבצעים באתר לא במבנה הצפוי; דילגתי עליו.")
    return out, (int(current) + 1) in parser.page_links and bool(out)


def parse_category(page):
    return parse_category_page(page, 1)[0]


# =========================================================================== interests (pure)

def _grocery():
    from grocery import kashrut, relevance   # vendored package (command-center/grocery)
    return kashrut, relevance


class Interest:
    __slots__ = ("name", "tier", "term", "item")

    def __init__(self, name, tier, term, item=None):
        self.name, self.tier, self.term, self.item = name, tier, term, item

    def as_dict(self):
        return {"name": self.name, "tier": self.tier, "term": self.term}


def build_interests(list_entries, regulars, staples, defaults=None):
    """Ordered, de-duplicated search interests. list_entries: [{"item", "text"}] (Google Tasks cache shape);
    regulars / staples: item names. Non-food items are skipped (no point scanning their deals)."""
    _kashrut, rel = _grocery()
    out, seen = [], set()

    def add(name, tier):
        name = " ".join(str(name or "").split())
        if not name or rel.item_is_nonfood(name):
            return
        item = None
        if defaults is not None:
            try:
                item = defaults.resolve(name)
            except Exception:
                item = None
        term = ((item.terms[0] if item is not None and item.terms else None) or rel.clean_text(name))[: sh.MAX_TERM]
        key = term.strip().lower()
        if key in seen:
            return
        seen.add(key)
        out.append(Interest(name, tier, term, item))

    for e in list_entries or []:
        add(e.get("item") or e.get("text"), TIER_LIST)
    for name in regulars or []:
        add(name, TIER_REGULAR)
    for name in staples or []:
        add(name, TIER_STAPLE)
    return out


def matches(interest, product):
    """Is this search hit the searched item (not just something the search returned)?"""
    kashrut_mod, rel = _grocery()
    name = product.get("name") or ""
    item = interest.item
    words = (item.match_words if item is not None else [interest.term]) or [interest.term]
    if not any(rel.rel_score(name, w)[0] >= 0.5 and rel.rel_score(name, w)[1] for w in words):
        return False
    norm = rel.N.norm_text(name)
    if item is not None:
        if item.require_any and not any(rel.N.norm_text(w) in norm for w in item.require_any):
            return False
        if item.exclude_words and any(rel.N.norm_text(w) in norm for w in item.exclude_words):
            return False
    return True


def is_nonfood(name):
    _k, rel = _grocery()
    n = rel.N.norm_text(name) + " "
    return any(w in n for w in rel.NONFOOD_NAME)


def kashrut_flag(name, level, produce=False):
    """None when fine / not required; "כשרות לא ידועה" when a rule applies and the name doesn't show it."""
    if not level or produce:
        return None
    kashrut_mod, _rel = _grocery()
    return None if level in kashrut_mod.name_level(name or "") else kashrut_mod.KASHRUT_UNKNOWN


# =========================================================================== the scan

def plan_requests(categories=None, max_pages=None, personal=False):
    """Worst case: account check (+ cart + mini-cart canary in personal mode) + every page + re-checks."""
    cats = len(enabled_categories() if categories is None else categories)
    pages = cats * (MAX_PAGES if max_pages is None else max(1, min(int(max_pages), HARD_MAX_PAGES)))
    rechecks = int(pages * sh.MIN_INTERVAL // sh.LOGIN_CHECK_TTL)   # the account check is trusted for 120 s
    total = (3 if personal else 1) + pages + rechecks
    return {"pages": pages, "requests": total,
            "seconds": int(total * sh.MIN_INTERVAL + 2 * total)}   # >= 10 s spacing + ~2 s per response


class SiteOps:
    """The real, read-only site calls (each one goes through super_hist's policy/throttle/breaker)."""

    def cart(self):
        with sh._lock:
            sh._require_login()
            return sh._read_cart()

    def category_page(self, key, page):
        url = CATEGORY_URLS[key] + (f"?page={int(page)}" if int(page) > 1 else "")
        with sh._lock:
            sh._require_login()
            return parse_category_page(sh._cart_send("GET", url, referer=sh.BASE + "/"), page)


class _SkipCart(Exception):
    pass


LOGIN_MAX_WAIT = 180          # seconds: one automatic login per run, bounded
LOGIN_POLL = 15               # each relay check waits up to this long (no site request while waiting)


class _LoginFailed(Exception):
    pass


class AutoLogin:
    """ONE automatic login per scan (Ben, 2026-10-21): the existing flow — super_hist.start_login asks the site
    for an SMS code and waits for the Telegram relay to deliver it; if it is not there yet, the relay is
    polled with submit_code() (empty code = take the relayed one; no site request until a code arrives) until
    LOGIN_MAX_WAIT. Never resends, never retries. Returns (ok, reason)."""

    def __init__(self, start=None, submit=None, clock=None, max_wait=None):
        self.start = start or sh.start_login
        self.submit = submit or sh.submit_code
        self.clock = clock or time.monotonic
        self.max_wait = LOGIN_MAX_WAIT if max_wait is None else max_wait
        self.calls = 0

    def attempt(self):
        self.calls += 1
        deadline = self.clock() + self.max_wait
        result = self.start({}) or {}
        status = result.get("status")
        while status in ("code_sent", "no_relayed_code") and self.clock() < deadline:
            wait = int(max(1, min(LOGIN_POLL, deadline - self.clock())))
            result = self.submit({"wait_seconds": wait}) or {}
            status = result.get("status")
        if status in ("logged_in", "already_logged_in"):
            return True, None
        if status in ("code_sent", "no_relayed_code"):
            return False, "קוד ה-SMS לא הגיע תוך 3 דקות"
        return False, sh._clip(result.get("text") or status or "שגיאה לא ידועה", 80)


class NoLogin:
    """For offline/fixture runs: never logs in."""

    calls = 0

    def attempt(self):
        self.calls += 1
        return False, "אין התחברות בריצה לא מקוונת"


def _cart_deals(cart):
    out = []
    for line in cart.get("lines") or []:
        shelf, unit = line.get("shelf_price"), line.get("unit_price")
        if shelf is not None and unit is not None and unit < shelf:
            out.append({"product_id": line["product_id"], "name": line["name"], "price": unit, "shelf_price": shelf,
                        "promo": None, "in_cart": True, "out_of_stock": False, "weighted": line.get("weighted"),
                        "url": product_link(line["product_id"])})
    return out


def run(list_entries=(), regulars=(), staples=(), defaults=None, kashrut_level=None, site=None,
        categories=None, max_pages=None, previous_ids=(), today=None, log=None, personal=None, login=None):
    """One deals scan. Default (standalone): scan the category pages only and rank the best-value deals —
    no grocery list, no cart, no regulars. personal=True (SUPER_HIST_DEALS_PERSONAL=1): also read the cart and
    match against the cached list/regulars/staples. Returns a JSON-able dict."""
    personal = PERSONAL_DEFAULT if personal is None else bool(personal)
    if not personal:
        list_entries, regulars, staples = (), (), ()
    site = site or SiteOps()
    log = log or (lambda _m: None)
    today = today or dt.date.today()
    categories = list(enabled_categories() if categories is None else categories)
    max_pages = MAX_PAGES if max_pages is None else max(1, min(int(max_pages), HARD_MAX_PAGES))
    level = kashrut_level if KASHRUT_ENABLED else None
    interests = build_interests(list_entries, regulars, staples, defaults)
    warnings, found, notable_pool = [], {}, {}
    stopped, requests, pages_read = None, 0, {}
    cart_ids = set()
    login = login or AutoLogin()
    logged = {"tried": False, "ok": False}

    def call(fn, *args):
        """A site read; logged out -> ONE automatic login, then the same read once more."""
        try:
            return fn(*args)
        except sh.LoginRequired:
            if logged["tried"]:
                raise _LoginFailed("החיבור נפל שוב אחרי ההתחברות") from None
            logged["tried"] = True
            log("logged out -> one automatic login")
            ok, reason = login.attempt()
            if not ok:
                raise _LoginFailed(reason) from None
            logged["ok"] = True
        try:
            return fn(*args)
        except sh.LoginRequired:
            raise _LoginFailed("עדיין לא מחובר אחרי ההתחברות") from None

    def failed(reason):
        res = {"status": "login_failed", "error": sh._clip(" ".join(str(reason).split()), 100),
               "deals": [], "notable": [], "auto_login": logged["tried"]}
        return _finish(res, requests, pages_read, interests, "login_failed", warnings, today)

    def keep(product, tier, interest_name=None, produce=False):
        d = deal_of(product, today)
        if d is None or d["out_of_stock"] or not d["product_id"]:
            return
        d["nonfood"] = is_nonfood(d["name"])
        if d["nonfood"] and tier != TIER_ALL:
            return
        d["tier"], d["why"], d["interest"] = tier, TIER_HE.get(tier), interest_name
        d["in_cart"] = d["in_cart"] or d["product_id"] in cart_ids
        d["kashrut_flag"] = kashrut_flag(d["name"], level, produce)
        d["new"] = d["product_id"] not in set(previous_ids or ()) if previous_ids else None
        pool = notable_pool if tier in (TIER_NOTABLE, TIER_ALL) else found
        old = found.get(d["product_id"]) or pool.get(d["product_id"])
        if old is None or (tier, -(d["pct_off"] or 0)) < (old["tier"], -(old["pct_off"] or 0)):
            pool[d["product_id"]] = d

    def classify(p):
        """Local matching of one category product against the cart, then the interests (list > regulars >
        staples). Unmatched -> other notable deals. Standalone mode: everything is just a deal."""
        if not personal:
            keep(p, TIER_ALL)
            return
        if p.get("product_id") in cart_ids or p.get("in_cart"):
            hit = next((it for it in interests if it.tier == TIER_LIST and matches(it, p)), None)
            keep(p, TIER_LIST if hit else TIER_CART, hit.name if hit else p.get("name"))
            return
        for it in interests:
            if matches(it, p):
                keep(p, it.tier, it.name, bool(it.item is not None and it.item.produce))
                return
        keep(p, TIER_NOTABLE)

    # 1. personal mode only: the cart (2-3 requests; logged out -> the one automatic login)
    try:
        if not personal:
            raise _SkipCart
        cart = call(site.cart)
        requests += 2
        cart_ids.update(ln["product_id"] for ln in cart.get("lines") or [])
        for p in _cart_deals(cart):
            keep(p, TIER_CART, p["name"])
    except _SkipCart:
        pass
    except _LoginFailed as exc:
        return failed(exc)
    except sh.SiteError as exc:
        return _finish({"status": _err_status(exc), "error": str(exc)[:200], "deals": [], "notable": []},
                       requests, pages_read, interests, _err_status(exc), warnings, today)

    # 2. the category pages, page 1..max_pages, following the site's own next-page link only
    for key in categories:
        page = 1
        while page <= max_pages and stopped is None:
            try:
                products, has_next = call(site.category_page, key, page)
                requests += 1
            except _LoginFailed as exc:
                return failed(exc)
            except sh.SiteChanged as exc:
                requests += 1
                warnings.append(f"{CATEGORY_HE.get(key, key)}: {exc}")
                break
            except sh.SiteError as exc:
                stopped = _err_status(exc)
                warnings.append(str(exc)[:200])
                break
            pages_read[key] = page
            for p in products:
                classify(p)
            log(f"{key} page {page}: {len(products)} products, next={has_next}")
            if not has_next:
                break
            page += 1
        if page > max_pages and pages_read.get(key) == max_pages:
            warnings.append(f"{CATEGORY_HE.get(key, key)}: נקראו רק {max_pages} עמודים (מגבלת עמודים).")
        if stopped:
            break

    if not personal:
        best = sorted((d for d in notable_pool.values() if d["pct_off"]),
                      key=lambda d: (d["nonfood"], -(d["pct_off"] or 0), d["name"]))
        result = {"status": "partial" if stopped else "ok", "stopped": stopped, "personal": False,
                  "auto_login": logged["ok"],
                  "deals": best[:TOP_DEALS], "notable": [], "categories": categories}
        return _finish(result, requests, pages_read, interests, None, warnings, today)
    mine = sorted(found.values(), key=lambda d: (d["tier"], d["kashrut_flag"] is not None, -(d["pct_off"] or 0),
                                                d["name"]))[:MINE_MAX]
    notable = [d for d in notable_pool.values() if d["product_id"] not in found
               and (d["pct_off"] or 0) >= NOTABLE_MIN_PCT]
    notable = sorted(notable, key=lambda d: (d["kashrut_flag"] is not None, -(d["pct_off"] or 0), d["name"]))
    result = {"status": "partial" if stopped else "ok", "stopped": stopped, "personal": True,
              "auto_login": logged["ok"], "deals": mine,
              "notable": notable[:NOTABLE_MAX], "categories": categories}
    return _finish(result, requests, pages_read, interests, None, warnings, today)


def _err_status(exc):
    return sh._error_result(exc)["status"]


def _finish(result, requests, pages_read, interests, error_status, warnings, today):
    result.setdefault("auto_login", False)
    result.update(requests=requests, pages=dict(pages_read), interests=[i.as_dict() for i in interests],
                  warnings=warnings, date=today.isoformat(), cart_url=sh.CART_URL)
    result["message"] = render(result, error_status)
    return result


# =========================================================================== Hebrew message (pure)

def _money(v):
    return sh._fmt(v)


def _deal_line(i, d):
    unit, shelf, terms = d.get("effective_unit"), d.get("shelf_price"), d.get("terms")
    if d.get("multi_n") and unit is not None:
        shown = terms if terms and not d.get("multi_total") else f"{d['multi_n']} ב-{_money(d['multi_total'])}"
        price = f"{shown} ({_money(unit)} ליח׳ במקום {_money(shelf)})"
    elif unit is not None and shelf is not None and unit < shelf:
        price = f"{_money(unit)} במקום {_money(shelf)}"
        if terms and d.get("sale_price") is not None and unit < d["sale_price"]:
            price += f" · {terms}"
    else:
        price = f"{terms or 'מבצע'} (מחיר מדף {_money(shelf)})"
    pct = f" (‎-{d['pct_off']}%)" if d.get("pct_off") else ""
    extra_cart = "בעגלה" if d.get("in_cart") and d.get("tier") not in (TIER_CART, TIER_NOTABLE) else None
    tags = [t for t in (d.get("why"), extra_cart, "חדש" if d.get("new") else None) if t]
    until = f"עד {dt.date.fromisoformat(d['until']).strftime('%d.%m')}" if d.get("until") else "תוקף לא מוצג"
    flag = f" 🔍 {d['kashrut_flag']}" if d.get("kashrut_flag") else ""
    weighted = " · במשקל" if d.get("weighted") else ""
    head = f"{i}. {sh._clip(d['name'], 60)} — {price}{pct}"
    meta = " · ".join(tags + [until])
    return f"{head}\n   {meta}{weighted}{flag}\n   {d.get('url') or ''}".rstrip()


def login_failed_line(reason):
    return f"🏷️ מבצעים בסופר החברתי: לא הצלחתי להתחבר אוטומטית ({reason}); עצרתי."


def render(result, error_status=None):
    date = dt.date.fromisoformat(result["date"]).strftime("%d.%m")
    if error_status == "login_failed":
        return login_failed_line(result.get("error") or "שגיאה")
    if error_status:
        return f"🏷️ מבצעים בסופר החברתי: לא הצלחתי לבדוק ({result.get('error') or error_status}). לא ניסיתי שוב."
    out = [f"🏷️ מבצעים בסופר החברתי — {date}"]
    mine, notable = result["deals"], result["notable"]
    if not result.get("personal", True):
        if mine:
            out += ["", f"המבצעים הכי משתלמים ({len(mine)}):"]
            out += [_deal_line(i, d) for i, d in enumerate(mine, 1)]
        else:
            out += ["", "לא מצאתי מבצעים בקטגוריות המבצעים."]
    elif mine:
        out += ["", f"על מה שאתה קונה ({len(mine)}):"]
        out += [_deal_line(i, d) for i, d in enumerate(mine, 1)]
    else:
        out += ["", "לא מצאתי מבצעים על מה שאתה קונה בדרך כלל."]
    if notable and result.get("personal", True):
        out += ["", f"עוד מבצעים בולטים ({len(notable)}):"]
        out += [_deal_line(i, d) for i, d in enumerate(notable, 1)]
    if KASHRUT_ENABLED:
        flagged = sum(1 for d in mine + notable if d.get("kashrut_flag"))
        if flagged:
            out += ["", f"🔍 {flagged} מבצעים בלי כשרות מהדרין/בד״ץ בשם המוצר — לבדוק בדף המוצר לפני קנייה."]
    read = ", ".join(f"{CATEGORY_HE.get(k, k)} ({n} עמ׳)" for k, n in (result.get("pages") or {}).items())
    out += ["", f"נסרקו: {read or 'אף עמוד'} · כ-{result['requests']} פניות לאתר."]
    if result.get("auto_login"):
        out.append("🔑 החיבור לאתר פג — התחברתי אוטומטית עם קוד SMS.")
    if result.get("stopped"):
        out.append(f"⚠️ עצרתי באמצע: {result['warnings'][-1] if result['warnings'] else result['stopped']}")
    elif result.get("warnings"):
        out.append("⚠️ " + " ".join(w for w in result["warnings"] if w)[:300])
    out.append("המחירים מהאתר בזמן הבדיקה; תוקף המבצעים לרוב לא מוצג באתר.")
    return "\n".join(out)


# =========================================================================== production sources

def _cached_list():
    """The grocery list from the Google Tasks CACHE (state/current_list.json) — no sync, no Tasks call."""
    from . import groceries
    path = groceries.STATE_DIR / "current_list.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], None
    return list(data.get("items") or []), data.get("synced_at")


def _sources():
    """(list_entries, regulars, staples, defaults, kashrut_level, warnings) from the Command Center state.
    Read-only and best-effort: a failing source is reported, never fatal."""
    warnings = []
    entries, regulars, staples, defaults, level = [], [], [], None, None
    try:
        entries, _synced = _cached_list()
    except Exception as exc:
        warnings.append(f"רשימת הקניות השמורה לא נטענה ({type(exc).__name__})")
    try:
        from . import groceries
        from grocery.defaults import load_defaults
        defaults = load_defaults(str(groceries.DEFAULTS))
        staples = list(defaults.aliases)
        level = defaults.global_prefs.get("kashrut") or None
    except Exception as exc:
        warnings.append(f"ההעדפות לא נטענו ({type(exc).__name__})")
    try:
        from .database import connection
        rows = connection.execute(
            "SELECT item, COUNT(DISTINCT run_id) c FROM grocery_run_lines WHERE item IS NOT NULL "
            "GROUP BY item HAVING c >= 2 ORDER BY c DESC LIMIT 10").fetchall()
        regulars = [r[0] for r in rows]
    except Exception:
        pass
    return entries, regulars, staples, defaults, level, warnings


def _persisted_block():
    try:
        from . import grocery_db
        from .database import connection
        return grocery_db.blocked(connection, SITE)
    except Exception:
        return None


def _persist_block(error):
    try:
        from . import grocery_db
        from .database import connection
        grocery_db.set_blocked(connection, SITE, error)
    except Exception:
        pass


def _previous_ids():
    try:
        data = json.loads((OUT_DIR / "last.json").read_text(encoding="utf-8"))
        return {d["product_id"] for d in (data.get("deals") or []) + (data.get("notable") or [])}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def _save(result):
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = OUT_DIR / "last.json.tmp"
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, OUT_DIR / "last.json")
        (OUT_DIR / "last.md").write_text(result["message"] + "\n", encoding="utf-8")
    except OSError as exc:
        result.setdefault("warnings", []).append(f"לא נשמר דוח ({type(exc).__name__})")


def _max_pages_arg(p):
    value = p.get("max_pages", MAX_PAGES)
    if not str(value).isdigit() or not 1 <= int(value) <= HARD_MAX_PAGES:
        raise ValueError(f"max_pages צריך להיות 1–{HARD_MAX_PAGES}.")
    return int(value)


def scan(payload=None):
    """Production scan: cached sources + persisted 24 h stop + save last report. Synchronous (minutes)."""
    p = payload or {}
    max_pages = _max_pages_arg(p)
    block = _persisted_block()
    if block:
        sh._blocked_until[0] = max(sh._blocked_until[0], float(block[0]))
    personal = bool(p.get("personal", PERSONAL_DEFAULT))
    if personal:
        entries, regulars, staples, defaults, level, warnings = _sources()
    else:   # standalone: no Google Tasks, no cart, no grocery DB
        entries, regulars, staples, defaults, level, warnings = [], [], [], None, None, []
    result = run(entries, regulars, staples, defaults, level, max_pages=max_pages, previous_ids=_previous_ids(),
                 personal=personal, log=lambda m: print(f"[super-hist-deals] {m}", flush=True))
    result["warnings"] = warnings + result.get("warnings", [])
    if result["status"] == "site_blocked" or result.get("stopped") == "site_blocked":
        _persist_block((result.get("warnings") or ["blocked"])[-1])
    if result["status"] in ("ok", "partial"):
        _save(result)
    return result


# =========================================================================== async job (API / MCP)

def _job(ident, payload):
    try:
        result = scan(payload)
        with _runs_lock:
            _runs[ident].update(status="done", finished=time.time(), result=result)
    except Exception as exc:   # recorded and logged, never swallowed
        print(f"[super-hist-deals] {ident} failed: {exc}\n{traceback.format_exc()}", flush=True)
        with _runs_lock:
            _runs[ident].update(status="failed", finished=time.time(), error=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        _job_lock.release()


def start(payload=None, _query=None):
    """Start a deals scan in the background -> {run_id, eta_seconds, planned}. One scan at a time."""
    p = payload or {}
    try:
        max_pages = _max_pages_arg(p)
    except ValueError as exc:
        return {"status": "invalid", "text": str(exc)}
    if not enabled_categories():
        return {"status": "disabled", "text": "אין קטגוריית מבצעים מופעלת (SUPER_HIST_DEALS_PROMO_PAGES)."}
    if sh._now() < sh._blocked_until[0] or _persisted_block():
        return {"status": "site_blocked", "text": "האתר חסם/ביקש אימות לאחרונה; לא שולח בקשות עד שיעברו 24 שעות."}
    if not _job_lock.acquire(blocking=False):
        running = next((k for k, v in _runs.items() if v["status"] == "running"), None)
        return {"status": "already_running", "run_id": running, "text": "בדיקת מבצעים כבר רצה; לבדוק סטטוס."}
    ident = os.urandom(6).hex()
    planned = plan_requests(max_pages=max_pages, personal=bool(p.get("personal", PERSONAL_DEFAULT)))
    with _runs_lock:
        _runs[ident] = {"status": "running", "started": time.time(), "finished": None, "result": None,
                        "error": None}
    threading.Thread(target=_job, args=(ident, dict(p)), daemon=True).start()
    return {"status": "running", "run_id": ident, "eta_seconds": planned["seconds"], "planned": planned,
            "text": f"בודק מבצעים בסופר החברתי (עד {planned['requests']} פניות, ~{max(1, round(planned['seconds'] / 60))} דק׳)."}


def status(payload=None, _query=None):
    p = payload or {}
    ident = str(p.get("run_id") or "")
    wait = p.get("wait_seconds", 0)
    deadline = time.time() + max(0, min(int(wait) if str(wait).isdigit() else 0, STATUS_WAIT_MAX))
    while True:
        with _runs_lock:
            run_ = dict(_runs.get(ident) or {})
        if not run_:
            return {"status": "unknown_run", "text": "לא מכיר את הריצה הזאת (אולי השרת הופעל מחדש). אפשר להפעיל שוב."}
        if run_["status"] != "running" or time.time() >= deadline:
            break
        time.sleep(2)
    if run_["status"] == "running" and time.time() - run_["started"] > STALE_SECONDS:
        return {"status": "failed", "run_id": ident, "text": "הבדיקה רצה יותר מדי זמן; עצרתי את המעקב."}
    if run_["status"] == "running":
        return {"status": "running", "run_id": ident, "seconds": int(time.time() - run_["started"])}
    if run_["status"] == "failed":
        return {"status": "failed", "run_id": ident, "text": f"הבדיקה נכשלה: {run_['error']}"}
    result = run_["result"]
    return {"run_id": ident, **{k: result[k] for k in result if k != "interests"}, "text": result["message"]}


def last(_payload=None, _query=None):
    """The previous saved report (no site traffic) — for a routine that only wants to re-send it."""
    try:
        data = json.loads((OUT_DIR / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "none", "text": "עוד לא נשמר דוח מבצעים."}
    return {k: data[k] for k in data if k != "interests"} | {"text": data.get("message", "")}


def register_routes(router):
    router.post("/v1/super-hist/deals", start, 202)
    router.post("/v1/super-hist/deals/status", status)
    router.get("/v1/super-hist/deals/last", last)


# =========================================================================== CLI

def main(argv=None):
    """CLI (routine-ready).
      Offline, SYNTHETIC fixtures (no network):
        python -m command_center.super_hist_deals --fixtures tests/fixtures/super-hist-deals [--json]
      Against the running Command Center (all site traffic stays in that one process, so its throttle and
      24 h stop apply; token from COMMAND_CENTER_TOKEN):
        python -m command_center.super_hist_deals --api http://command-center:8080/v1 [--max-pages 5] [--json]
    Prints the Hebrew message (or the JSON result). Exit 0 ok/partial, 2 login_failed, 3 blocked/error."""
    import argparse
    ap = argparse.ArgumentParser(prog="super_hist_deals")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixtures", help="directory with SYNTHETIC fixtures (offline)")
    src.add_argument("--api", help="Command Center base URL, e.g. http://command-center:8080/v1")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES)
    ap.add_argument("--personal", action="store_true", help="also match against list/cart/regulars")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.fixtures:
        result = run_fixtures(a.fixtures, a.max_pages, personal=a.personal)
    else:
        from .mcp_tools.client import CommandCenterClient
        client = CommandCenterClient(a.api)
        started = client.request("POST", "/super-hist/deals", {"max_pages": a.max_pages, "personal": a.personal},
                                 timeout=60)
        if started.get("status") != "running":
            result = started
        else:
            while True:
                result = client.request("POST", "/super-hist/deals/status",
                                        {"run_id": started["run_id"], "wait_seconds": 60}, timeout=120)
                if result.get("status") != "running":
                    break
    print(json.dumps(result, ensure_ascii=False, indent=1) if a.json else (result.get("message") or result.get("text")))
    st = result.get("status")
    return 0 if st in ("ok", "partial") else 2 if st == "login_failed" else 3


class FixtureSite:
    """Offline site from a directory (all SYNTHETIC): cart.html and <category>-p<N>.html pages."""

    def __init__(self, root):
        self.root = Path(root)
        self.calls = []

    def cart(self):
        self.calls.append(("GET", "cart"))
        return sh.parse_cart((self.root / "cart.html").read_text(encoding="utf-8"))

    def category_page(self, key, page):
        self.calls.append(("GET", key, page))
        f = self.root / f"{key}-p{page}.html"
        return parse_category_page(f.read_text(encoding="utf-8") if f.exists() else "", page)


def run_fixtures(root, max_pages=None, today=None, categories=None, personal=False):
    root = Path(root)
    spec = json.loads((root / "scenario.json").read_text(encoding="utf-8"))
    defaults = None
    if spec.get("defaults"):
        from grocery.defaults import load_defaults
        defaults = load_defaults(str((root / spec["defaults"]).resolve()), user_path=str(root / "none.yaml"))
    return run([{"item": t} for t in spec.get("list", [])], spec.get("regulars", []),
               spec.get("staples", []), defaults, spec.get("kashrut"), site=FixtureSite(root),
               categories=categories if categories is not None else spec.get("categories", ["promo"]),
               max_pages=max_pages, previous_ids=set(spec.get("previous_ids", [])), personal=personal,
               login=NoLogin(),
               today=dt.date.fromisoformat(spec["today"]) if spec.get("today") else today)


if __name__ == "__main__":
    raise SystemExit(main())
