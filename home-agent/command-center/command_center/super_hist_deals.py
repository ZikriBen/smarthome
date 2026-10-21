"""הסופר החברתי (super-hist.co.il) deals scan — READ-ONLY. "Don't miss the promotions on what Ben buys."

What it reads (all through super_hist's transport: URL allowlist, >= MIN_INTERVAL between requests, the 24 h
stop on 403/429/captcha, never an automatic login):
  1. Ben's cart page (1 request, +1 account check, +1 mini-cart only when the cart reads empty). A cart line
     whose .price_span shows <del>shelf</del><ins>sale</ins> is a deal with no extra request.
  2. One POST /search-results/ per "interest" term, in this order, capped by `max_searches`:
       grocery-list lines (Google Tasks "קניות")  ->  regulars (items in >= 2 past cart runs)  ->
       staples (the curated defaults.yaml items).
     The search JSON (keys verified live 2026-10-20) carries `price` (shelf), `special` (sale price, false when
     none) and `bgfnribbon_text` (promo ribbon, false when none; e.g. "2 ב-10" — the TEXT format is assumed).
     The site returns only the top 5 hits per search; hits that are not the searched item but are food on a
     big discount become "other notable deals".
  3. OPTIONAL, off by default (markup not verified live): the promotions category page
     /משלמים-פחות-מקבלים-יותר/ (page 1 only) for more "other notable deals".
Nothing here writes to the site, the cart or the grocery list.

Ranking: (1) deals on things Ben buys — on the list > in the cart > regulars > staples; within a tier by % off;
(2) up to NOTABLE_MAX other food deals with >= NOTABLE_MIN_PCT off. Non-food is skipped. Kashrut: the site has
no kashrut field, so the rule is read from the product name like Shufersal (kashrut.name_level): a name saying
בד"ץ/מהדרין is "ok", anything else is "כשרות לא ידועה" (shown with 🔍, never silently dropped); produce is exempt.
Dates: the search JSON has no validity dates; a date written in the ribbon text is shown, else "תוקף לא מוצג".

Entry points: run(...) (pure, injectable), start()/status() (async job for the API/MCP: a scan takes minutes),
last() (the previous report), and `python -m command_center.super_hist_deals` (CLI; see main()).
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
DEFAULT_MAX_SEARCHES = 12
HARD_MAX_SEARCHES = 25
STATUS_WAIT_MAX = 90
STALE_SECONDS = 20 * 60
PROMO_PATH = "/משלמים-פחות-מקבלים-יותר/"
PROMO_URL = sh.BASE + urllib.parse.quote(PROMO_PATH)
PROMO_PAGES_DEFAULT = os.environ.get("SUPER_HIST_DEALS_PROMO_PAGES", "0") == "1"
OUT_DIR = Path(os.environ.get("SUPER_HIST_DEALS_DIR", "/workspace/grocery/super_hist_deals"))
SITE = "super_hist"

TIER_LIST, TIER_CART, TIER_REGULAR, TIER_STAPLE, TIER_NOTABLE = 1, 2, 3, 4, 9
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


def parse_category(page):
    """Promotions category HTML -> products in the parse_search shape. Raises SiteChanged when there are no
    product cards at all (markup differs from the assumption: the scan then just skips this source)."""
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
    if not out:
        raise sh.SiteChanged("דף המבצעים באתר לא במבנה הצפוי; דילגתי עליו.")
    return out


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

class Budget:
    def __init__(self, max_searches):
        self.max_searches = max(0, min(int(max_searches), HARD_MAX_SEARCHES))
        self.requests = 0
        self.searches = 0


def plan_requests(n_interests, max_searches=DEFAULT_MAX_SEARCHES, promo_pages=False):
    """Worst case: account check + cart (+ mini-cart canary) + searches + promo page + account re-checks."""
    searches = min(n_interests, max(0, min(int(max_searches), HARD_MAX_SEARCHES)))
    reads = searches + (1 if promo_pages else 0)
    rechecks = int(reads * sh.MIN_INTERVAL // sh.LOGIN_CHECK_TTL)   # the account check is trusted for 120 s
    total = 3 + reads + rechecks
    return {"searches": searches, "requests": total,
            "seconds": int(total * sh.MIN_INTERVAL + 2 * total)}   # >= 10 s spacing + ~2 s per response


class SiteOps:
    """The real, read-only site calls (each one goes through super_hist's policy/throttle/breaker)."""

    def cart(self):
        with sh._lock:
            sh._require_login()
            return sh._read_cart()

    def search(self, term):
        with sh._lock:
            sh._require_login()
            found = sh.parse_search(sh._cart_send("POST", sh.SEARCH_URL,
                                                  {"search": term, "screen_break_points": "desktop"},
                                                  referer=sh.BASE + "/"))
        now = sh._now()
        for p in found["products"]:
            sh._catalog[p["product_id"]] = (now, p)
        return found["products"]

    def promo_page(self):
        with sh._lock:
            sh._require_login()
            return parse_category(sh._cart_send("GET", PROMO_URL, referer=sh.BASE + "/"))


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
        max_searches=DEFAULT_MAX_SEARCHES, promo_pages=False, previous_ids=(), today=None, log=None):
    """One deals scan. Returns a JSON-able dict with status, message (Hebrew), mine/notable deals, budget."""
    site = site or SiteOps()
    log = log or (lambda _m: None)
    today = today or dt.date.today()
    interests = build_interests(list_entries, regulars, staples, defaults)
    budget = Budget(max_searches)
    warnings, found, notable_pool = [], {}, {}
    stopped = None
    cart_ids = set()

    def keep(product, tier, interest_name=None, produce=False, level=None):
        d = deal_of(product, today)
        if d is None or d["out_of_stock"] or not d["product_id"]:
            return
        if is_nonfood(d["name"]):
            return
        d["tier"], d["why"] = tier, TIER_HE.get(tier)
        d["interest"] = interest_name
        d["in_cart"] = d["in_cart"] or d["product_id"] in cart_ids
        d["kashrut_flag"] = kashrut_flag(d["name"], level, produce)
        d["new"] = d["product_id"] not in set(previous_ids or ()) if previous_ids else None
        pool = notable_pool if tier == TIER_NOTABLE else found
        old = found.get(d["product_id"]) or pool.get(d["product_id"])
        if old is None or (tier, -(d["pct_off"] or 0)) < (old["tier"], -(old["pct_off"] or 0)):
            pool[d["product_id"]] = d

    # 1. the cart (also tells us whether the session is alive)
    try:
        cart = site.cart()
        budget.requests += 2
        cart_ids.update(ln["product_id"] for ln in cart.get("lines") or [])
        for p in _cart_deals(cart):
            keep(p, TIER_CART, p["name"], level=kashrut_level)
    except sh.LoginRequired:
        return _finish({"status": "login_required", "deals": [], "notable": [], "interests": len(interests)},
                       budget, interests, [], "login_required", warnings, today)
    except sh.SiteError as exc:
        return _finish({"status": _err_status(exc), "error": str(exc)[:200], "deals": [], "notable": []},
                       budget, interests, [], _err_status(exc), warnings, today)

    # 2. one search per interest, within the budget
    searched, skipped = [], []
    for it in interests:
        if budget.searches >= budget.max_searches:
            skipped.append(it.name)
            continue
        try:
            products = site.search(it.term)
        except sh.SiteError as exc:
            stopped = _err_status(exc)
            warnings.append(str(exc)[:200])
            skipped.extend(i.name for i in interests[interests.index(it):])
            break
        budget.searches += 1
        budget.requests += 1
        searched.append(it.name)
        level = (it.item.kashrut if it.item is not None and it.item.kashrut is not None else kashrut_level)
        produce = bool(it.item is not None and it.item.produce)
        for p in products:
            if matches(it, p):
                tier = TIER_CART if (p.get("in_cart") or p.get("product_id") in cart_ids) and it.tier > TIER_CART \
                    else it.tier
                keep(p, tier, it.name, produce, level)
            else:
                keep(p, TIER_NOTABLE, None, False, kashrut_level)
        log(f"searched {it.term!r}: {len(products)} hits")

    # 3. optional promotions page
    if promo_pages and stopped is None:
        try:
            for p in site.promo_page():
                keep(p, TIER_NOTABLE, None, False, kashrut_level)
            budget.requests += 1
        except sh.SiteChanged as exc:
            budget.requests += 1
            warnings.append(str(exc))
        except sh.SiteError as exc:
            stopped = _err_status(exc)
            warnings.append(str(exc)[:200])

    mine = sorted(found.values(), key=lambda d: (d["tier"], d["kashrut_flag"] is not None, -(d["pct_off"] or 0),
                                                d["name"]))[:MINE_MAX]
    notable = [d for d in notable_pool.values() if d["product_id"] not in found
               and (d["pct_off"] or 0) >= NOTABLE_MIN_PCT]
    notable = sorted(notable, key=lambda d: (d["kashrut_flag"] is not None, -(d["pct_off"] or 0), d["name"]))
    notable = notable[:NOTABLE_MAX]
    result = {"status": "partial" if stopped else "ok", "stopped": stopped, "deals": mine, "notable": notable,
              "searched": searched, "not_searched": skipped}
    return _finish(result, budget, interests, searched, None, warnings, today)


def _err_status(exc):
    return sh._error_result(exc)["status"]


def _finish(result, budget, interests, searched, error_status, warnings, today):
    result.update(requests=budget.requests, searches=budget.searches, max_searches=budget.max_searches,
                  interests=[i.as_dict() for i in interests], warnings=warnings, date=today.isoformat(),
                  cart_url=sh.CART_URL)
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


def render(result, error_status=None):
    date = dt.date.fromisoformat(result["date"]).strftime("%d.%m")
    if error_status == "login_required":
        return ("🏷️ מבצעים בסופר החברתי: החיבור לאתר פג, אז לא בדקתי מבצעים. "
                "לשלוח קוד SMS להתחברות? (רק באישורך)")
    if error_status:
        return f"🏷️ מבצעים בסופר החברתי: לא הצלחתי לבדוק ({result.get('error') or error_status}). לא ניסיתי שוב."
    out = [f"🏷️ מבצעים בסופר החברתי — {date}"]
    mine, notable = result["deals"], result["notable"]
    if mine:
        out += ["", f"על מה שאתה קונה ({len(mine)}):"]
        out += [_deal_line(i, d) for i, d in enumerate(mine, 1)]
    else:
        out += ["", "לא מצאתי מבצעים על מה שאתה קונה בדרך כלל."]
    if notable:
        out += ["", f"עוד מבצעים בולטים ({len(notable)}):"]
        out += [_deal_line(i, d) for i, d in enumerate(notable, 1)]
    flagged = sum(1 for d in mine + notable if d.get("kashrut_flag"))
    if flagged:
        out += ["", f"🔍 {flagged} מבצעים בלי כשרות מהדרין/בד״ץ בשם המוצר — לבדוק בדף המוצר לפני קנייה."]
    foot = f"נבדקו {result['searches']} מוצרים (כ-{result['requests']} פניות לאתר)"
    if result.get("not_searched"):
        foot += f"; לא נבדקו: {', '.join(result['not_searched'][:8])}" + ("…" if len(result["not_searched"]) > 8 else "")
    out += ["", foot + "."]
    if result.get("stopped"):
        out.append(f"⚠️ עצרתי באמצע: {result['warnings'][-1] if result['warnings'] else result['stopped']}")
    out.append("המחירים מהאתר בזמן הבדיקה; תוקף המבצעים לרוב לא מוצג באתר.")
    return "\n".join(out)


# =========================================================================== production sources

def _sources():
    """(list_entries, regulars, staples, defaults, kashrut_level, warnings) from the Command Center state.
    Every source is best-effort: a failing one is reported, never fatal."""
    warnings = []
    entries, regulars, staples, defaults, level = [], [], [], None, None
    try:
        from . import groceries, grocery_list
        with groceries._lock:
            st, warn, _n = grocery_list.sync()
        entries = list(st.get("items") or [])
        if warn:
            warnings.append(warn)
        from grocery.defaults import load_defaults
        defaults = load_defaults(str(groceries.DEFAULTS))
        staples = list(defaults.aliases)
        level = defaults.global_prefs.get("kashrut") or None
    except Exception as exc:
        warnings.append(f"רשימת הקניות/ההעדפות לא נטענו ({type(exc).__name__})")
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


def scan(payload=None):
    """Production scan: sources + persisted 24 h stop + save last report. Synchronous (minutes)."""
    p = payload or {}
    max_searches = p.get("max_searches", DEFAULT_MAX_SEARCHES)
    if not str(max_searches).isdigit():
        raise ValueError("max_searches must be a whole number")
    block = _persisted_block()
    if block:
        sh._blocked_until[0] = max(sh._blocked_until[0], float(block[0]))
    entries, regulars, staples, defaults, level, warnings = _sources()
    extra = [str(t) for t in (p.get("extra_terms") or []) if str(t).strip()][:10]
    entries = [{"item": t} for t in extra] + entries
    result = run(entries, regulars, staples, defaults, level, max_searches=int(max_searches),
                 promo_pages=bool(p.get("promo_pages", PROMO_PAGES_DEFAULT)), previous_ids=_previous_ids(),
                 log=lambda m: print(f"[super-hist-deals] {m}", flush=True))
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
    max_searches = p.get("max_searches", DEFAULT_MAX_SEARCHES)
    if not str(max_searches).isdigit() or int(max_searches) > HARD_MAX_SEARCHES:
        return {"status": "invalid", "text": f"max_searches צריך להיות 0–{HARD_MAX_SEARCHES}."}
    if sh._now() < sh._blocked_until[0] or _persisted_block():
        return {"status": "site_blocked", "text": "האתר חסם/ביקש אימות לאחרונה; לא שולח בקשות עד שיעברו 24 שעות."}
    if not _job_lock.acquire(blocking=False):
        running = next((k for k, v in _runs.items() if v["status"] == "running"), None)
        return {"status": "already_running", "run_id": running, "text": "בדיקת מבצעים כבר רצה; לבדוק סטטוס."}
    ident = os.urandom(6).hex()
    planned = plan_requests(DEFAULT_MAX_SEARCHES if p.get("max_searches") is None else int(max_searches),
                            int(max_searches), bool(p.get("promo_pages", PROMO_PAGES_DEFAULT)))
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
        python -m command_center.super_hist_deals --api http://command-center:8080/v1 [--max-searches 12] [--json]
    Prints the Hebrew message (or the JSON result). Exit 0 ok/partial, 2 login_required, 3 blocked/error."""
    import argparse
    ap = argparse.ArgumentParser(prog="super_hist_deals")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixtures", help="directory with SYNTHETIC fixtures (offline)")
    src.add_argument("--api", help="Command Center base URL, e.g. http://command-center:8080/v1")
    ap.add_argument("--max-searches", type=int, default=DEFAULT_MAX_SEARCHES)
    ap.add_argument("--promo-pages", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.fixtures:
        result = run_fixtures(a.fixtures, a.max_searches, a.promo_pages)
    else:
        from .mcp_tools.client import CommandCenterClient
        client = CommandCenterClient(a.api)
        started = client.request("POST", "/super-hist/deals",
                                 {"max_searches": a.max_searches, "promo_pages": a.promo_pages}, timeout=60)
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
    return 0 if st in ("ok", "partial") else 2 if st == "login_required" else 3


class FixtureSite:
    """Offline site from a directory: cart.html, search/<term>.json, promo.html (all SYNTHETIC)."""

    def __init__(self, root):
        self.root = Path(root)
        self.calls = []

    def cart(self):
        self.calls.append(("GET", "cart"))
        return sh.parse_cart((self.root / "cart.html").read_text(encoding="utf-8"))

    def search(self, term):
        self.calls.append(("POST", term))
        f = self.root / "search" / f"{term}.json"
        if not f.exists():
            return []
        return sh.parse_search(f.read_text(encoding="utf-8"))["products"]

    def promo_page(self):
        self.calls.append(("GET", "promo"))
        return parse_category((self.root / "promo.html").read_text(encoding="utf-8"))


def run_fixtures(root, max_searches=DEFAULT_MAX_SEARCHES, promo_pages=False, today=None):
    root = Path(root)
    spec = json.loads((root / "scenario.json").read_text(encoding="utf-8"))
    defaults = None
    if spec.get("defaults"):
        from grocery.defaults import load_defaults
        defaults = load_defaults(str((root / spec["defaults"]).resolve()), user_path=str(root / "none.yaml"))
    return run([{"item": t} for t in spec.get("list", [])], spec.get("regulars", []),
               spec.get("staples", []), defaults, spec.get("kashrut"), site=FixtureSite(root),
               max_searches=max_searches, promo_pages=promo_pages, previous_ids=set(spec.get("previous_ids", [])),
               today=dt.date.fromisoformat(spec["today"]) if spec.get("today") else today)


if __name__ == "__main__":
    raise SystemExit(main())
