"""Histadrut "הסופר החברתי" (super-hist.co.il) login with a one-time SMS code.

The session lives HERE (server-side cookie jar), never in Hermes's browser: the site's login is two plain jQuery
AJAX posts (``account/login/sms`` with the ID number, then ``account/login/sms_validate`` with the 6-digit code),
so no browser is needed and a multi-minute wait for Ben to paste the code costs nothing.

Privacy: the ID number is read from a root-only secret file and is never returned, logged, or sent to Hermes.
The code is used once and never stored or logged. Only status/metadata leaves this module.
Politeness: at most one request to the site every MIN_INTERVAL seconds; no automatic resend or retry.
"""

import html
import html.parser
import http.cookiejar
import importlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import otp_inbox

BASE = "https://super-hist.co.il"
LOGIN_PAGE = BASE + "/login/"
SMS_URL = BASE + "/index.php?route=account/login/sms"
VALIDATE_URL = BASE + "/index.php?route=account/login/sms_validate"
ACCOUNT_URL = BASE + "/index.php?route=account/account"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0 Safari/537.36")

DATA_DIR = Path(os.environ.get("SUPER_HIST_DATA_DIR", "/data/super_hist"))
VAT_ID_FILE = Path(os.environ.get("SUPER_HIST_VAT_ID_FILE", str(DATA_DIR / "vat_id")))
COOKIES_FILE = DATA_DIR / "cookies.txt"

MIN_INTERVAL = 10.0        # seconds between requests to the site
RESEND_COOLDOWN = 60       # the site's own resend timer
CODE_WINDOW = 10 * 60      # a pending login is abandoned after this
MAX_CODE_ATTEMPTS = 3
CODE_RE = re.compile(r"\d{6}")
# Telegram auto-relay (Hermes otp-relay plugin -> shared inbox). Disabled when the inbox dir is not mounted.
INBOX_DB = Path(os.environ.get("OTP_INBOX_DB", "/otp-inbox/codes.sqlite"))
AUTO_WAIT = 45             # seconds start_login waits for a relayed code before asking Ben to paste
MAX_WAIT = 60
POLL = 2.0
SITE = "super_hist"

# --------------------------------------------------------------------------- URL policy (enforced in _send)
# Every request to the site goes through _send, and _send refuses anything outside this allowlist BEFORE it
# touches the network (redirect targets included). Checkout, payment, orders, coupons, shipping and
# cart/clear can never be reached, not even with a GET.
SEARCH_PATH = "/search-results/"
CART_URL = BASE + "/index.php?route=checkout/cart"
CART_INFO_URL = BASE + "/index.php?route=common/cart/info"
CART_ADD_URL = BASE + "/index.php?route=checkout/cart/add"
CART_REMOVE_URL = BASE + "/index.php?route=checkout/cart/remove"
CART_EDIT_URL = BASE + "/index.php?route=checkout/cart/edit"
SEARCH_URL = BASE + SEARCH_PATH
CART_ROUTES = {"checkout/cart", "checkout/cart/add", "checkout/cart/remove", "checkout/cart/edit"}
GET_ROUTES = {"account/account", "checkout/cart", "common/cart/info"}
POST_ROUTES = {"account/login/sms", "account/login/sms_validate",
               "checkout/cart/add", "checkout/cart/remove", "checkout/cart/edit"}
GET_PATHS = {"/", "/login/"}
POST_PATHS = {SEARCH_PATH}
BLOCKED_WORDS = re.compile(
    r"checkout|cart/clear|coupon|voucher|payment|order|confirm|shipping|success|/pay|=pay|register|guest", re.I)
# Bot-challenge / captcha markers (Cloudflare interstitial, reCAPTCHA/hCaptcha/Turnstile widgets).
CHALLENGE_RE = re.compile(r"cf_chl_|challenge-platform|cf-challenge|Just a moment\.\.\.|Attention Required|"
                          r"class=[\"'][^\"']*\b(?:g-recaptcha|h-captcha|cf-turnstile)\b", re.I)

_lock = threading.Lock()
_state = {"phase": "idle", "sent_at": 0.0, "started_at": 0.0, "attempts": 0, "logged_in_at": 0.0, "checked_at": 0.0}
_last_request = [0.0]
_jar = None
_sleep = time.sleep          # tests replace these two
_now = time.time


class SiteError(RuntimeError):
    pass


class PolicyError(SiteError):
    """A request the URL policy forbids. Raised before anything is sent."""


def _route(query):
    return (urllib.parse.parse_qs(query).get("route") or [""])[0]


def check_url(method, url):
    """Raise PolicyError unless (method, url) is an allowlisted request to the site. Pure; no I/O."""
    base = urllib.parse.urlsplit(BASE)
    u = urllib.parse.urlsplit(url)
    if u.scheme != base.scheme or u.hostname != base.hostname or u.port != base.port or u.username or u.password:
        raise PolicyError("בקשה לאתר אחר נחסמה")
    route = _route(u.query)
    whole = urllib.parse.unquote(u.path + "?" + u.query).lower()
    # The exact allowlisted cart routes are the only place the word "checkout" may appear.
    scrubbed = whole.replace("route=" + route, "", 1) if route in CART_ROUTES and u.path == "/index.php" else whole
    if BLOCKED_WORDS.search(scrubbed):
        raise PolicyError("נתיב חסום (קופה/תשלום/הזמנה/קופון/משלוח) לא נשלח")
    if method == "GET":
        ok = (u.path in GET_PATHS and not u.query) or (u.path == "/index.php" and route in GET_ROUTES)
    elif method == "POST":
        ok = (u.path in POST_PATHS and not u.query) or (u.path == "/index.php" and route in POST_ROUTES)
    else:
        ok = False
    if not ok:
        raise PolicyError(f"בקשה שאינה ברשימה המותרת נחסמה ({method})")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class _PolicyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to an allowlisted GET; anything else (e.g. to checkout) stops the request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url("GET", urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _cookie_jar():
    global _jar
    if _jar is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _jar = http.cookiejar.MozillaCookieJar(str(COOKIES_FILE))
        if COOKIES_FILE.exists():
            try:
                _jar.load(ignore_discard=True, ignore_expires=False)
            except (OSError, http.cookiejar.LoadError):
                _jar.clear()
    return _jar


def _save_cookies():
    jar = _cookie_jar()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    jar.save(ignore_discard=True, ignore_expires=False)
    os.chmod(COOKIES_FILE, 0o600)


def _throttle():
    wait = MIN_INTERVAL - (_now() - _last_request[0])
    if wait > 0:
        _sleep(wait)
    _last_request[0] = _now()


def _send(method, url, form=None, ajax=False, follow=True, referer=None):
    """One HTTP request with the shared cookie jar. Returns (status, headers, text). Never raises on HTTP status.
    The URL policy is checked first (PolicyError, nothing sent), then the shared >=MIN_INTERVAL throttle."""
    check_url(method, url)
    _throttle()
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {"User-Agent": UA, "Accept-Language": "he-IL,he;q=0.9"}
    if ajax:
        headers.update({"X-Requested-With": "XMLHttpRequest", "Referer": referer or LOGIN_PAGE, "Origin": BASE,
                        "Accept": "application/json, text/javascript, */*; q=0.01"})
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    handlers = [urllib.request.HTTPCookieProcessor(_cookie_jar())]
    handlers.append(_PolicyRedirect() if follow else _NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read(3_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(200_000).decode("utf-8", "replace")
    except PolicyError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SiteError(f"האתר לא זמין כרגע ({type(exc).__name__})") from None


def _json(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _clean(text, limit=200):
    """Site messages are HTML-ish Hebrew; strip tags, collapse spaces, and never echo long digit runs."""
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    text = re.sub(r"\d{7,}", "***", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _read_vat_id():
    try:
        value = VAT_ID_FILE.read_text().strip()
    except OSError:
        return None
    return value if re.fullmatch(r"\d{5,9}", value) else None


def _is_logged_in():
    status, headers, _ = _send("GET", ACCOUNT_URL, follow=False)
    if status in (301, 302, 303, 307, 308):
        return False
    return status == 200


def _result(status, text, **extra):
    out = {"status": status, "text": text}
    out.update(extra)
    return out


def _inbox_enabled():
    return INBOX_DB.parent.is_dir()


def _wait_for_relayed_code(seconds):
    """Poll the shared inbox for a code relayed after this login began (single use). The stale-code guard is
    the moment start_login began, BEFORE the SMS request: a fast SMS can land while the POST is still in flight
    or while start_login is still waiting. The inbox is never cleared at start; older codes are just ignored
    (and expire by TTL)."""
    deadline = _now() + max(0, min(int(seconds), MAX_WAIT))
    since = _state.get("started_at") or _state["sent_at"]
    while True:
        try:
            code = otp_inbox.take(INBOX_DB, SITE, since=since, now=_now())
        except Exception:
            code = None
        if code or _now() >= deadline:
            return code
        _sleep(POLL)


def _auto_submit(seconds):
    code = _wait_for_relayed_code(seconds)
    if not code:
        return None
    result = submit_code({"code": code})
    del code
    result["auto"] = True
    if result["status"] == "logged_in":
        result["text"] = "קיבלתי את הקוד אוטומטית מה-SMS. " + result["text"]
    return result


def start_login(payload=None, _query=None):
    """Ask the site to SMS a one-time code; with the relay inbox mounted, wait up to `wait_seconds` for the code
    to arrive by itself and submit it. Never resends automatically."""
    result = _start_login_locked()
    if result["status"] != "code_sent" or not _inbox_enabled():
        return result
    wait = (payload or {}).get("wait_seconds", AUTO_WAIT)
    auto = _auto_submit(wait) if str(wait).isdigit() and int(wait) > 0 else None
    if auto is not None:
        return auto
    result["text"] += " הקוד לא הגיע אוטומטית; תדביק אותו כאן."
    result["auto_relay"] = "no_code_yet"
    return result


def _start_login_locked():
    with _lock:
        vat_id = _read_vat_id()
        if not vat_id:
            return _result("not_configured", "חסר מספר תעודת זהות בהגדרות השרת (קובץ סודי). צריך להגדיר אותו פעם אחת בשרת.")
        now = _now()
        if _state["phase"] == "code_sent" and now - _state["sent_at"] < RESEND_COOLDOWN:
            left = int(RESEND_COOLDOWN - (now - _state["sent_at"]))
            return _result("cooldown", f"קוד כבר נשלח לפני פחות מדקה. אפשר לבקש שוב בעוד {left} שניות.",
                           retry_after_seconds=left)
        started_at = now   # before any request to the site; the relay accepts codes stored from here on
        try:
            if _state["phase"] == "logged_in" and _is_logged_in():
                return _result("already_logged_in", "כבר מחובר לסופר החברתי.")
            status, _, page = _send("GET", LOGIN_PAGE)
            if status != 200 or 'id="login-sms-form"' not in page or 'name="vat_id"' not in page:
                return _result("site_changed", "דף ההתחברות של האתר השתנה או חסום, לא שלחתי כלום. צריך לבדוק ידנית.",
                               http_status=status)
            status, headers, body = _send("POST", SMS_URL, {"form_type": "sms", "vat_id": vat_id}, ajax=True)
        except SiteError as exc:
            return _result("site_error", str(exc))
        data = _json(body) or {}
        if data.get("success"):
            _state.update(phase="code_sent", sent_at=_now(), started_at=started_at, attempts=0)
            _save_cookies()
            return _result("code_sent", _clean(data.get("message")) or "נשלח קוד חד פעמי ב-SMS.",
                           expires_in_seconds=CODE_WINDOW)
        if data.get("redirect"):
            # The site skips the code when the session is already authenticated.
            if _is_logged_in():
                _state.update(phase="logged_in", logged_in_at=_now())
                _save_cookies()
                return _result("already_logged_in", "כבר מחובר לסופר החברתי.")
            return _result("site_error", "האתר הפנה לדף אחר בלי לשלוח קוד.")
        if data.get("error") == "rate_limited" or status == 429:
            retry = data.get("retry_after_seconds")
            remaining = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
            return _result("rate_limited", "האתר חסם זמנית שליחת קודים (יותר מדי ניסיונות). לא לנסות שוב עכשיו.",
                           retry_after_seconds=int(retry) if str(retry or "").isdigit() else None,
                           attempts_remaining=remaining)
        return _result("rejected", _clean(data.get("error")) or f"האתר לא שלח קוד (HTTP {status}).")


def submit_code(payload, _query=None):
    """Submit the 6-digit code Ben pasted. Used once; never stored or logged."""
    code = re.sub(r"[\s\-]", "", str((payload or {}).get("code") or ""))
    if not code and _inbox_enabled():
        # No pasted code: take one the relay delivered (waits up to wait_seconds, default 30).
        if _state["phase"] != "code_sent":
            return _result("no_pending_login", "אין התחברות שמחכה לקוד. צריך להתחיל התחברות מחדש.")
        wait = (payload or {}).get("wait_seconds", 30)
        auto = _auto_submit(wait if str(wait).isdigit() else 30)
        return auto or _result("no_relayed_code", "לא הגיע קוד אוטומטי. תדביק את הקוד מה-SMS.")
    if not CODE_RE.fullmatch(code):
        return _result("invalid_code", "הקוד צריך להיות 6 ספרות.")
    with _lock:
        if _state["phase"] != "code_sent" or _now() - _state["sent_at"] > CODE_WINDOW:
            _state["phase"] = "idle" if _state["phase"] == "code_sent" else _state["phase"]
            return _result("no_pending_login", "אין התחברות שמחכה לקוד (או שעברו יותר מ-10 דקות). צריך להתחיל התחברות מחדש.")
        try:
            status, _, body = _send("POST", VALIDATE_URL, {"form_type": "sms", "code": code}, ajax=True)
            del code
            data = _json(body) or {}
            ok = bool(data.get("redirect")) and _is_logged_in()
        except SiteError as exc:
            return _result("site_error", str(exc))
        if ok:
            _state.update(phase="logged_in", logged_in_at=_now(), attempts=0, checked_at=_now())
            _save_cookies()
            return _result("logged_in", "מחובר לסופר החברתי.")
        _state["attempts"] += 1
        if _state["attempts"] >= MAX_CODE_ATTEMPTS:
            _state.update(phase="idle", attempts=0)
            return _result("too_many_attempts", "הקוד נדחה 3 פעמים. עצרתי; צריך להתחיל התחברות מחדש.")
        return _result("wrong_code", _clean(data.get("error")) or f"האתר דחה את הקוד (HTTP {status}).",
                       attempts_left=MAX_CODE_ATTEMPTS - _state["attempts"])


def status(_payload=None, _query=None):
    """Is the stored session logged in? One read-only request to the account page."""
    with _lock:
        configured = _read_vat_id() is not None
        pending = _state["phase"] == "code_sent" and _now() - _state["sent_at"] <= CODE_WINDOW
        try:
            logged_in = _is_logged_in()
        except SiteError as exc:
            return _result("site_error", str(exc), configured=configured, pending_code=pending)
        if logged_in:
            _state.update(phase="logged_in", checked_at=_now())
            _save_cookies()
        elif _state["phase"] == "logged_in":
            _state["phase"] = "idle"
        relay = _inbox_enabled()
        text = ("מחובר לסופר החברתי." if logged_in else
                "ממתין לקוד מ-SMS." if pending else "לא מחובר לסופר החברתי.")
        return _result("logged_in" if logged_in else ("code_sent" if pending else "logged_out"), text,
                       configured=configured, pending_code=pending, auto_relay=relay)


def logout(_payload=None, _query=None):
    """Forget the local session (cookie jar). Does not contact the site."""
    with _lock:
        _cookie_jar().clear()
        try:
            COOKIES_FILE.unlink()
        except FileNotFoundError:
            pass
        _state.update(phase="idle", sent_at=0.0, started_at=0.0, attempts=0, logged_in_at=0.0, checked_at=0.0)
        _catalog.clear()
        return _result("logged_out", "ניתקתי את החיבור השמור לסופר החברתי.")


# =========================================================================== cart (view / search / writes)
# The cart is ACCOUNT-level: it is the same cart Ben sees on the site and in the app, so every write is
# visible there immediately and Ben may be editing it in parallel. Hence:
#   * reads never write; writes only run as an approved Command Center action (approvals.register_action);
#   * the write handler re-reads the cart first and refuses if the line changed since the proposal;
#   * after a write the cart is read again and the real quantity is reported (no blind success);
#   * a POST is never retried; 403/429/challenge stops all cart traffic (BLOCK_SECONDS);
#   * an expired session returns login_required — never an automatic login (an SMS needs Ben).
# Semantics: add(product, N) = "make sure the cart has at least N" (not in cart -> add N; fewer than N ->
# set_qty N; N or more -> nothing to do). set_qty(product, N) = exactly N (N >= 1). remove = drop the line.

CART_ACTION = "super_hist_cart_write"
LOGIN_CHECK_TTL = 120        # seconds a successful account-page check is trusted
BLOCK_SECONDS = 24 * 3600    # stop all cart traffic after a 403/429/challenge (in memory; a restart clears it)
CATALOG_TTL = 30 * 60        # product facts remembered from a search, for proposals
MAX_QTY = 20
MAX_TERM = 60
SEARCH_LIMIT = 10
_catalog = {}                # product_id -> (ts, product dict) from recent searches / cart reads
_blocked_until = [0.0]


class LoginRequired(SiteError):
    pass


class SiteBlocked(SiteError):
    pass


class SiteChanged(SiteError):
    pass


class CartChanged(SiteError):
    pass


def _approvals():
    # Imported lazily: approvals opens the Command Center database; tests inject a private one.
    return importlib.import_module(".approvals", __package__)


def _money(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 2)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value or "").replace("\u200f", "").replace("\u200e", ""))
    return round(float(m.group(0).replace(",", "")), 2) if m else None


def _fmt(amount):
    return "?" if amount is None else f"₪{amount:.2f}"


def _truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _qty(value):
    try:
        number = float(str(value).strip() or "0")
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _pid(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"\d{1,10}", value):
        raise ValueError("product_id חייב להיות מספר מוצר מהחיפוש או מהעגלה")
    return value


# --------------------------------------------------------------------------- parsing (pure)

class _CartParser(html.parser.HTMLParser):
    """Lines: div.cart_product[data-product_id][data-cart-id]; name .product-description a; UNIT price
    .price_span ins (sale) / del (shelf) or the span text (verified live: sum(unit x qty) == the cart total;
    the page repeats .price_span for mobile, the first number wins); qty input[name=quantity]. Totals:
    .sub_total / .final_sum.total -> .total_text."""

    VOID = {"input", "img", "br", "hr", "meta", "link", "source", "wbr", "area", "base", "col", "embed"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.lines, self.totals = [], [], {}
        self.line = None

    def _in(self, cls):
        return any(cls in classes for _tag, classes, _mark in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        mark = None
        if "cart_product" in classes and a.get("data-product_id"):
            self.line = {"product_id": a.get("data-product_id"), "cart_id": a.get("data-cart-id"),
                         "name": "", "price_text": "", "sale_text": "", "shelf_text": "", "qty_text": None}
            mark = "line"
        if self.line is not None and tag == "input" and a.get("name") == "quantity":
            self.line["qty_text"] = a.get("value")
        if tag in self.VOID:
            return
        for key in ("sub_total", "final_sum"):
            if key in classes:
                mark = mark or key
        self.stack.append((tag, classes, mark))

    def handle_endtag(self, tag):
        # Stray / self-closing end tags (the real page writes <img ... />, which html.parser reports as an end
        # tag too) must not unwind the stack: only close an element that is actually open.
        if tag in self.VOID or not any(t == tag for t, _c, _m in self.stack):
            return
        while self.stack:
            t, _classes, mark = self.stack.pop()
            if mark == "line" and self.line is not None:
                self.lines.append(self.line)
                self.line = None
            if t == tag:
                break

    def handle_data(self, data):
        if self.line is not None:
            if self._in("product-description") and self.stack and self.stack[-1][0] == "a":
                self.line["name"] += data
            elif self._in("price_span"):
                tag = self.stack[-1][0] if self.stack else ""
                key = "sale_text" if tag == "ins" else "shelf_text" if tag == "del" else "price_text"
                self.line[key] += data
        if self._in("total_text"):
            for key in ("final_sum", "sub_total"):
                if any(mark == key for _t, _c, mark in self.stack):
                    self.totals[key] = self.totals.get(key, "") + data
                    break


def parse_cart(page):
    """Cart page HTML -> {"lines": [...], "subtotal", "total", "count"}. Raises SiteChanged when the page has
    no cart markers at all (theme change / not a cart page)."""
    parser = _CartParser()
    parser.feed(page or "")
    parser.close()
    lines = []
    for raw in parser.lines:
        qty = _qty(raw["qty_text"])
        sale, shelf, plain = _money(raw["sale_text"]), _money(raw["shelf_text"]), _money(raw["price_text"])
        lines.append({"product_id": str(raw["product_id"]), "cart_id": str(raw["cart_id"] or ""),
                      "name": _clean(html.unescape(raw["name"]), 120), "quantity": qty,
                      "unit_price": sale if sale is not None else plain if plain is not None else shelf,
                      "shelf_price": shelf, "weighted": qty is None or not isinstance(qty, int)})
    subtotal, total = _money(parser.totals.get("sub_total")), _money(parser.totals.get("final_sum"))
    if any(not line["cart_id"] or line["quantity"] is None for line in lines):
        raise SiteChanged("מבנה שורות העגלה באתר השתנה; לא כותב כלום עד שיבדקו את הקוד.")
    if not lines and total is None and subtotal is None and "cart_product" not in (page or ""):
        if not re.search(r"route=checkout/cart|id=[\"']cart|class=[\"'][^\"']*\bcart", page or ""):
            raise SiteChanged("זה לא נראה כמו דף העגלה של האתר.")
    return {"lines": lines, "subtotal": subtotal, "total": total if total is not None else subtotal,
            "count": len(lines)}


def parse_cart_info(fragment):
    """common/cart/info fragment: <input type="hidden" class="hidden_total" value="N פריט\\ים - ₪X"> -> (N, X).
    N counts UNITS (sum of quantities), not lines (live 2026-10-20: 30 for 22 lines)."""
    text = None
    for tag in re.finditer(r"<[^>]*\bclass=[\"'][^\"']*\bhidden_total\b[^\"']*[\"'][^>]*>", fragment or ""):
        value = re.search(r"\bvalue=[\"']([^\"']*)[\"']", tag.group(0))   # <input class="hidden_total" value=…>
        if value:
            text = value.group(1)
        else:                                                              # <span class="hidden_total">…</span>
            after = re.match(r"([^<]*)<", (fragment or "")[tag.end():])
            text = after.group(1) if after else None
        if text and text.strip():
            break
    if not text or not text.strip():
        raise SiteChanged("סיכום העגלה הקטן באתר השתנה.")
    text = html.unescape(text)
    count = re.match(r"\s*(\d+)", text)
    return (int(count.group(1)) if count else None), _money(text.split("-", 1)[-1] if "-" in text else None)


def _product_url(href):
    """Only on-site https links are handed back."""
    u = urllib.parse.urlsplit(str(href or ""))
    return str(href) if u.scheme == "https" and u.hostname == "super-hist.co.il" else None


def parse_search(body):
    """POST /search-results/ JSON (sent as text/html) -> normalized products. Effective price = special when
    present (price_number is the SHELF price, never the price paid)."""
    data = _json(body)
    if data is None or not isinstance(data.get("data", []), list):
        raise SiteChanged("תשובת החיפוש באתר השתנתה (לא JSON צפוי).")
    out = []
    for item in data.get("data") or []:
        if not isinstance(item, dict) or not str(item.get("product_id", "")).isdigit():
            continue
        shelf = _money(item.get("price"))
        special = _money(item.get("special")) if item.get("special") not in (None, "", False, "0") else None
        minimum = _qty(item.get("minimum") or 1)
        # Live 2026-10-20: `weight` is a boolean flag (false on every packaged product seen; no by-weight
        # product came up yet), next to `weight_unit_price_text` ('' when not by weight). Either one, or a
        # fractional minimum, marks a by-weight product.
        weighted = (bool(str(item.get("weight_unit_price_text") or "").strip()) or _truthy(item.get("weight"))
                    or not isinstance(minimum, int))
        out.append({"product_id": str(item["product_id"]), "name": _clean(html.unescape(str(item.get("name") or "")), 120),
                    "price": special if special is not None else shelf, "shelf_price": shelf,
                    "on_sale": special is not None and shelf is not None and special < shelf,
                    # bgfnribbon_text is `false` (bool) when there is no ribbon
                    "promo": _clean(item["bgfnribbon_text"], 60) or None
                    if isinstance(item.get("bgfnribbon_text"), str) else None,
                    "in_cart": _truthy(item.get("in_cart")), "out_of_stock": _truthy(item.get("out_of_stock")),
                    "minimum": minimum if isinstance(minimum, int) and minimum > 0 else 1,
                    "weighted": weighted, "url": _product_url(item.get("href"))})
    results = data.get("results")
    return {"results": int(results) if str(results or "").isdigit() else len(out), "products": out}


# --------------------------------------------------------------------------- transport for cart calls

def _cart_send(method, url, form=None, referer=None):
    """One cart-side request: breaker, policy+throttle (via _send), no redirects, stop on block/challenge."""
    if _now() < _blocked_until[0]:
        raise SiteBlocked("האתר חסם/ביקש אימות קודם; עצרתי את כל פעולות העגלה לזמן מה. לא לנסות שוב עכשיו.")
    status, headers, body = _send(method, url, form=form, ajax=True, follow=False, referer=referer or CART_URL)
    if status in (403, 429) or CHALLENGE_RE.search(body or ""):
        _blocked_until[0] = _now() + BLOCK_SECONDS
        raise SiteBlocked(f"האתר חסם את הבקשה (HTTP {status} / אימות אנושי). עצרתי בלי לנסות שוב.")
    if status in (301, 302, 303, 307, 308):
        location = (headers.get("Location") or headers.get("location") or "").lower()
        if "login" in location or "account" in location:
            _expire_session()
            raise LoginRequired("login")
        raise SiteChanged(f"האתר הפנה לדף אחר (HTTP {status}); לא המשכתי.")
    if status != 200:
        raise SiteError(f"האתר החזיר HTTP {status}.")
    return body


def _expire_session():
    if _state["phase"] == "logged_in":
        _state["phase"] = "idle"
    _state["checked_at"] = 0.0


def _require_login(fresh=False):
    """Trust a recent successful check; otherwise one read-only account-page request. Never logs in."""
    if not fresh and _state.get("checked_at") and _now() - _state["checked_at"] < LOGIN_CHECK_TTL:
        return
    if _now() < _blocked_until[0]:
        raise SiteBlocked("האתר חסם/ביקש אימות קודם; עצרתי את כל פעולות העגלה לזמן מה.")
    if not COOKIES_FILE.exists() and not len(_cookie_jar()):
        _expire_session()
        raise LoginRequired("login")
    status, _headers, body = _send("GET", ACCOUNT_URL, follow=False)
    if status in (403, 429) or CHALLENGE_RE.search(body or ""):
        _blocked_until[0] = _now() + BLOCK_SECONDS
        raise SiteBlocked(f"האתר חסם את הבקשה (HTTP {status} / אימות אנושי). עצרתי בלי לנסות שוב.")
    if status != 200:
        _expire_session()
        raise LoginRequired("login")
    _state.update(phase="logged_in", checked_at=_now())


def _read_cart(check_mini=True):
    cart = parse_cart(_cart_send("GET", CART_URL, referer=BASE + "/"))
    if not cart["lines"] and check_mini:
        # Canary: an "empty" parse must agree with the mini-cart; otherwise the theme changed under us.
        count, _total = parse_cart_info(_cart_send("GET", CART_INFO_URL))
        if count:
            raise SiteChanged(f"דף העגלה נקרא כריק אבל האתר מדווח {count} פריטים; כנראה המבנה השתנה.")
    now = _now()
    for line in cart["lines"]:
        known = _catalog.get(line["product_id"], (0, {}))[1]
        _catalog[line["product_id"]] = (now, dict(known, product_id=line["product_id"], name=line["name"] or known.get("name", ""),
                                                  price=known.get("price", line["unit_price"])))
    return cart


def _line(cart, product_id):
    return next((line for line in cart["lines"] if line["product_id"] == product_id), None)


def _error_result(exc):
    if isinstance(exc, LoginRequired):
        return _result("login_required", "החיבור לסופר החברתי פג. צריך להתחבר מחדש (SMS) — רק אם בן מאשר; "
                       "לא התחברתי אוטומטית.")
    if isinstance(exc, PolicyError):
        return _result("blocked_by_policy", str(exc))
    if isinstance(exc, SiteBlocked):
        return _result("site_blocked", str(exc))
    if isinstance(exc, SiteChanged):
        return _result("site_changed", str(exc))
    return _result("site_error", str(exc)[:200])


def _public_cart(cart):
    return {"count": cart["count"], "subtotal": cart["subtotal"], "total": cart["total"],
            "lines": [{k: line[k] for k in ("product_id", "name", "quantity", "unit_price", "shelf_price", "weighted")}
                      for line in cart["lines"]]}


# --------------------------------------------------------------------------- read-only tools

def cart_view(_payload=None, _query=None):
    """Read-only: the account cart's lines, quantities and total (1–3 requests)."""
    with _lock:
        try:
            _require_login()
            cart = _read_cart()
        except SiteError as exc:
            return _error_result(exc)
    text = (f"בעגלה בסופר החברתי {cart['count']} שורות, סה״כ {_fmt(cart['total'])}." if cart["count"]
            else "העגלה בסופר החברתי ריקה.")
    return _result("ok", text, cart=_public_cart(cart),
                   cart_url=CART_URL, note="העגלה משותפת לאתר ולאפליקציה; התשלום רק אצל בן.")


def search(payload, _query=None):
    """Read-only product search (one request). Products are remembered for CATALOG_TTL for proposals."""
    term = " ".join(str((payload or {}).get("term") or (payload or {}).get("query") or "").split())
    if not term or len(term) > MAX_TERM:
        return _result("invalid", f"צריך מונח חיפוש (עד {MAX_TERM} תווים).")
    with _lock:
        try:
            _require_login()
            found = parse_search(_cart_send("POST", SEARCH_URL, {"search": term, "screen_break_points": "desktop"},
                                            referer=BASE + "/"))
        except SiteError as exc:
            return _error_result(exc)
    now = _now()
    for product in found["products"]:
        _catalog[product["product_id"]] = (now, product)
    products = found["products"][:SEARCH_LIMIT]
    return _result("ok", f"נמצאו {found['results']} תוצאות; מוצגות {len(products)}.", term=term,
                   results=found["results"], products=products)


# --------------------------------------------------------------------------- proposals (no site writes)

def _known_product(product_id):
    ts, product = _catalog.get(product_id, (0, None))
    return product if product and _now() - ts <= CATALOG_TTL else None


def _summary(op, name, unit_price, before, after, cart):
    total = cart["total"]
    expected = None
    if total is not None and unit_price is not None:
        expected = round(total + unit_price * (after - before), 2)
    verb = {"add": "הוספה לעגלה", "set_qty": "עדכון כמות בעגלה", "remove": "הסרה מהעגלה"}[op]
    line = (f"הסופר החברתי: {verb} ״{_clip(name, 45)}״ · {_fmt(unit_price)} ליח׳ · כמות {before}→{after} · "
            f"סה״כ עגלה {_fmt(total)}→≈{_fmt(expected)} (העגלה משותפת לאפליקציה)")
    return line, expected


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _propose(op, payload):
    try:
        product_id = _pid((payload or {}).get("product_id"))
    except ValueError as exc:
        return _result("invalid", str(exc))
    qty = (payload or {}).get("quantity", 1 if op == "add" else None)
    if op != "remove":
        if not str(qty).isdigit() or not 1 <= int(qty) <= MAX_QTY:
            return _result("invalid", f"כמות צריכה להיות מספר שלם בין 1 ל-{MAX_QTY}"
                           + (" (להסרה: super_hist_cart_remove)." if str(qty) == "0" else "."))
        qty = int(qty)
    with _lock:
        try:
            _require_login()
            cart = _read_cart()
        except SiteError as exc:
            return _error_result(exc)
    line = _line(cart, product_id)
    product = _known_product(product_id) or {}
    before = line["quantity"] if line else 0
    if op == "add":
        if line is None and not product:
            return _result("search_first", "לא מכיר את המוצר הזה; צריך קודם super_hist_search כדי לראות שם ומחיר.")
        if product.get("out_of_stock") and line is None:
            return _result("out_of_stock", f"״{product.get('name')}״ אזל באתר; לא הצעתי הוספה.")
        if before and before >= qty:
            return _result("already_in_cart", f"״{line['name']}״ כבר בעגלה בכמות {before}; לא צריך שינוי.",
                           quantity=before)
        if before:
            op = "set_qty"    # never a blind add on an existing line: add on the site INCREASES the quantity
        minimum = int(product.get("minimum") or 1)
        if line is None and qty < minimum:
            return _result("invalid", f"כמות מינימלית למוצר הזה היא {minimum}.")
    if line is None and op in ("set_qty", "remove"):
        return _result("not_in_cart", "המוצר לא נמצא בעגלה כרגע; לא הצעתי שינוי.")
    if product.get("weighted") or (line and line["weighted"]):
        if op != "remove":
            return _result("weighted_unsupported", "מוצר במשקל: עוד לא אומת איך האתר מקבל כמות במשקל, אז לא כותב. "
                           "לבדיקה ידנית באתר.")
    after = 0 if op == "remove" else qty
    if after == before:
        return _result("no_change", f"הכמות כבר {before}; לא צריך שינוי.")
    name = (line or {}).get("name") or product.get("name") or f"מוצר {product_id}"
    unit_price = product.get("price") if product.get("price") is not None else (line or {}).get("unit_price")
    summary, expected = _summary(op, name, unit_price, before, after, cart)
    proposal = {"op": op, "product_id": product_id, "cart_id": (line or {}).get("cart_id"), "name": name,
                "unit_price": unit_price, "qty_before": before, "qty_after": after,
                "total_before": cart["total"], "expected_total": expected, "summary": summary}
    ident = _approvals().create(CART_ACTION, proposal)
    return _result("pending_approval", summary, approval_id=ident, proposal={k: v for k, v in proposal.items()
                                                                          if k != "cart_id"})


def propose_add(payload, _query=None):
    return _propose("add", payload)


def propose_set_qty(payload, _query=None):
    return _propose("set_qty", payload)


def propose_remove(payload, _query=None):
    return _propose("remove", payload)


# --------------------------------------------------------------------------- approved write (the only writer)

def _check_write_response(op, body, qty_after):
    """Live 2026-10-20: add -> {success: <html str>, cart_id, product_id, price, total: 'N פריט\\ים - ₪X',
    total_for_small_cart, current_product_quantity:int}; edit -> {success: true, cart_id, total, ...} (its total
    lagged the real cart); remove -> {total, total_for_small_cart, countProducts (units), product_id, ...}.
    The re-read cart, not these totals, is what decides success."""
    data = _json(body)
    if data is None:
        return "האתר לא החזיר JSON צפוי"
    if data.get("error"):
        return _clean(data.get("error") if isinstance(data.get("error"), str) else json.dumps(data["error"],
                                                                                          ensure_ascii=False))
    if op == "add":
        if not data.get("success"):
            return "האתר לא אישר את ההוספה"
        current = data.get("current_product_quantity")
        if current is not None and _qty(current) != qty_after:
            return f"האתר דיווח כמות {current} במקום {qty_after}"
    if op == "remove" and "total" not in data and "countProducts" not in data:
        return "תשובת ההסרה לא כוללת סיכום עגלה"
    return None


def apply_write(payload):
    """Approved action handler. Re-reads the cart, refuses if the line changed since the proposal, sends ONE
    write POST (never retried), then re-reads the cart and verifies. Raises on any failure (the approval is
    marked failed with the reason; on a partial failure the reason says what the cart now holds)."""
    p = payload if isinstance(payload, dict) else {}
    op, product_id = p.get("op"), _pid(p.get("product_id"))
    before, after = _qty(p.get("qty_before") or 0), _qty(p.get("qty_after") or 0)
    if op not in ("add", "set_qty", "remove") or before is None or after is None or not 0 <= after <= MAX_QTY:
        raise ValueError("הצעת עגלה לא תקינה")
    with _lock:
        try:
            _require_login(fresh=True)
            cart = _read_cart()
        except LoginRequired:
            raise LoginRequired("החיבור לסופר החברתי פג; לא שיניתי כלום. צריך להתחבר מחדש (SMS) ולהציע שוב.") from None
        line = _line(cart, product_id)
        now_qty = line["quantity"] if line else 0
        if now_qty != before:
            raise CartChanged(f"העגלה השתנתה מאז ההצעה (כמות {now_qty} במקום {before}). לא שיניתי כלום; להציע שוב.")
        if line is None and op != "add":
            raise CartChanged("המוצר כבר לא בעגלה. לא שיניתי כלום.")
        if op == "add" and line is None:
            url, form = CART_ADD_URL, {"product_id": product_id, "quantity": str(after)}
        elif op == "remove":
            url, form = CART_REMOVE_URL, {"key": line["cart_id"], "product_id": product_id}
        else:  # set_qty, or add onto an existing line
            url, form = CART_EDIT_URL, {"data[0][key]": line["cart_id"], "data[0][quantity]": str(after)}
        sent_error = None
        try:
            body = _cart_send("POST", url, form)
            problem = _check_write_response("add" if url == CART_ADD_URL else op, body, after)
        except SiteBlocked as exc:
            raise SiteBlocked(f"{exc} ייתכן שהעגלה השתנתה — לבדוק באתר או ב-super_hist_cart_view אחרי ההמתנה.") from None
        except SiteError as exc:   # the POST may or may not have reached the site
            sent_error, problem = exc, str(exc)
        try:
            final = _read_cart()
        except SiteError as exc:
            raise SiteError(f"{'הכתיבה נשלחה' if sent_error is None else 'ייתכן שהכתיבה נשלחה'}, אבל לא הצלחתי "
                            f"לקרוא שוב את העגלה ({exc}). ייתכן שהעגלה השתנתה — לבדוק עם super_hist_cart_view.") from None
        got = _line(final, product_id)
        got_qty = got["quantity"] if got else 0
        if got_qty != after:
            raise CartChanged(f"ייתכן שהעגלה השתנתה: ביקשתי כמות {after} ובפועל {got_qty}"
                              + (f" ({problem})" if problem else "") + f". סה״כ עגלה עכשיו {_fmt(final['total'])}.")
    return {"status": "done", "op": op, "product_id": product_id, "name": p.get("name"),
            "quantity": got_qty, "total": final["total"], "count": final["count"],
            "note": problem}


# =========================================================================== grocery weekly basket hooks
# The grocery picker (vendored `grocery` package) searches Super-Hist through grocery_transport(); the weekly
# job checks the login first with grocery_gate(); with build mode the proposed lines become ONE batched
# approval (propose_batch); the approved batch runs in the background (apply_batch) because N writes at
# >=10 s apart take longer than the Telegram decide call may wait.

CART_BATCH_ACTION = "super_hist_cart_batch"
MAX_BATCH = 40
_batches = {}               # batch id (approval id) -> latest result dict
_batch_listeners = []       # callables(run_id, result) — Command Center stores the result on the grocery run


def search_raw(term):
    """One read-only search for the grocery picker: the raw `data` list (product dicts as the site sends them).
    Raises LoginRequired / SiteBlocked / SiteChanged / SiteError like the cart tools."""
    term = " ".join(str(term or "").split())[:MAX_TERM]
    if not term:
        return []
    with _lock:
        _require_login()
        body = _cart_send("POST", SEARCH_URL, {"search": term, "screen_break_points": "desktop"}, referer=BASE + "/")
    data = _json(body)
    if data is None or not isinstance(data.get("data", []), list):
        raise SiteChanged("תשובת החיפוש באתר השתנתה (לא JSON צפוי).")
    now = _now()
    for product in parse_search(body)["products"]:
        _catalog[product["product_id"]] = (now, product)
    return [x for x in (data.get("data") or []) if isinstance(x, dict)]


def grocery_transport(term):
    """search_raw with errors mapped to the grocery package's (so the picker stops / skips correctly)."""
    from grocery.http import CloudflareBlocked, HttpError, SessionExpired
    try:
        return search_raw(term)
    except LoginRequired:
        raise SessionExpired("login_required: החיבור לסופר החברתי פג") from None
    except SiteBlocked as exc:
        raise CloudflareBlocked(str(exc)[:160]) from None
    except SiteError as exc:          # SiteChanged / PolicyError / network: this one search failed
        raise HttpError(str(exc)[:160]) from None


LOGIN_BUDGET = 180          # seconds the weekly run waits for an automatic login (start + relayed code)
LOGIN_POLL = 5.0            # local state / relay inbox poll (no site request)
LOGIN_FAIL = {"not_configured": "חסר מספר ת״ז בשרת", "cooldown": "קוד נשלח לפני פחות מדקה",
              "site_changed": "דף ההתחברות השתנה", "site_error": "שגיאת אתר", "rate_limited": "האתר חסם שליחת קודים",
              "rejected": "האתר לא שלח קוד", "wrong_code": "הקוד נדחה", "too_many_attempts": "הקוד נדחה 3 פעמים",
              "no_pending_login": "פג תוקף הקוד", "invalid_code": "קוד לא תקין"}


def _login_failed(reason):
    return _result("login_failed", f"🔐 ההתחברות האוטומטית לסופר החברתי נכשלה ({reason})", login="failed")


def grocery_gate(budget=None):
    """Before every weekly run (on demand or a routine): make sure Super-Hist is logged in.

    Logged in → go. Logged out → ONE automatic login per run: start_login (one SMS; the code arrives through the
    #OTP relay and is submitted by itself, or Ben pastes it → super_hist_login_code), then wait for the session
    up to LOGIN_BUDGET (3 min) by polling the local login state and the relay inbox (no site requests while
    waiting), then one verifying read. Never a second SMS, never a retry. A code already pending from an
    earlier start is waited for instead of sending another. Returns {"status": logged_in | login_failed |
    site_blocked | site_error, "text", "login": none|auto|failed, "requests"}."""
    budget = LOGIN_BUDGET if budget is None else budget
    t0 = _now()
    with _lock:
        try:
            _require_login(fresh=True)
            return _result("logged_in", "מחובר לסופר החברתי.", login="none", requests=1)
        except LoginRequired:
            pending = _state["phase"] == "code_sent" and _now() - _state["sent_at"] <= CODE_WINDOW
        except SiteBlocked as exc:
            return _result("site_blocked", str(exc), requests=1)
        except SiteError as exc:
            return _result("site_error", str(exc)[:200], requests=1)
    requests = 1
    if not pending:
        started = start_login({"wait_seconds": min(AUTO_WAIT, max(0, int(budget)))})
        requests += 2
        st = started.get("status")
        if st in ("logged_in", "already_logged_in"):
            return _result("logged_in", "התחברתי לסופר החברתי אוטומטית.", login="auto", requests=requests + 2)
        if st != "code_sent":
            return dict(_login_failed(LOGIN_FAIL.get(st, st)), requests=requests)
    deadline = t0 + budget
    while True:                                       # wait: relay inbox (auto-submit) or Ben's pasted code
        if _state["phase"] == "logged_in":
            break
        if _state["phase"] != "code_sent":            # e.g. 3 wrong codes / window passed
            return dict(_login_failed("הקוד נדחה או פג"), requests=requests)
        if _inbox_enabled():
            auto = _auto_submit(0)
            if auto is not None:
                requests += 1
                if auto.get("status") == "logged_in":
                    break
                if auto.get("status") not in ("wrong_code",):
                    return dict(_login_failed(LOGIN_FAIL.get(auto.get("status"), auto.get("status"))),
                                requests=requests)
        if _now() >= deadline:
            return dict(_login_failed(f"לא הגיע קוד תוך {int(budget // 60) or 1} דק׳"), requests=requests)
        _sleep(min(LOGIN_POLL, max(0.0, deadline - _now())) or LOGIN_POLL)
    return _result("logged_in", "התחברתי לסופר החברתי אוטומטית.", login="auto", requests=requests)


def _batch_qty(value):
    try:
        q = float(value)
    except (TypeError, ValueError):
        return None
    return int(q) if q.is_integer() and 1 <= q <= MAX_QTY else None


def propose_batch(lines, run_id=None):
    """ONE approval for the whole Super-Hist part of a weekly basket. lines: [{product_id, quantity, name,
    price, weighted}]. Reads the cart first (shown as-is), proposes add / raise-to-quantity per line, refuses
    by-weight lines (manual), never lowers or removes anything. Nothing is written here."""
    if not isinstance(lines, list) or not lines:
        return _result("invalid", "אין שורות להצעה.")
    with _lock:
        try:
            _require_login()
            cart = _read_cart()
        except SiteError as exc:
            return _error_result(exc)
    changes, skipped = [], []
    for raw in lines[:MAX_BATCH]:
        raw = raw if isinstance(raw, dict) else {}
        name = _clip(raw.get("name") or "", 60)
        try:
            pid = _pid(raw.get("product_id"))
        except ValueError:
            skipped.append({"name": name, "reason": "אין מספר מוצר"})
            continue
        if raw.get("weighted"):
            skipped.append({"product_id": pid, "name": name, "reason": "במשקל — להוסיף ידנית באתר"})
            continue
        qty = _batch_qty(raw.get("quantity"))
        if qty is None:
            skipped.append({"product_id": pid, "name": name, "reason": f"כמות לא נתמכת ({raw.get('quantity')})"})
            continue
        product = _known_product(pid) or {}
        if product.get("weighted"):
            skipped.append({"product_id": pid, "name": name, "reason": "במשקל — להוסיף ידנית באתר"})
            continue
        line = _line(cart, pid)
        before = line["quantity"] if line else 0
        if line is not None and line["weighted"]:
            skipped.append({"product_id": pid, "name": name, "reason": "במשקל — להוסיף ידנית באתר"})
            continue
        if line is None and product.get("out_of_stock"):
            skipped.append({"product_id": pid, "name": name, "reason": "אזל באתר"})
            continue
        if line is None:
            qty = max(qty, int(product.get("minimum") or 1))
        if before >= qty:
            skipped.append({"product_id": pid, "name": name, "reason": f"כבר בעגלה ({before})"})
            continue
        price = raw.get("price") if raw.get("price") is not None else product.get("price")
        changes.append({"op": "set_qty" if line else "add", "product_id": pid,
                        "cart_id": (line or {}).get("cart_id"), "name": name or product.get("name") or pid,
                        "unit_price": price, "qty_before": before, "qty_after": qty})
    if len(lines) > MAX_BATCH:
        skipped += [{"name": _clip((x or {}).get("name"), 60), "reason": f"מעל {MAX_BATCH} שורות"}
                    for x in lines[MAX_BATCH:]]
    now_txt = (f"בעגלה עכשיו {cart['count']} שורות, סה״כ {_fmt(cart['total'])}." if cart["count"]
               else "העגלה בסופר החברתי ריקה כרגע.")
    if not changes:
        return _result("no_changes", now_txt + " אין מה להוסיף.", cart=_public_cart(cart), skipped=skipped)
    delta = sum((c["unit_price"] or 0) * (c["qty_after"] - c["qty_before"]) for c in changes)
    expected = round(cart["total"] + delta, 2) if cart["total"] is not None else None
    adds = sum(1 for c in changes if c["op"] == "add")
    eta = int((len(changes) + 3) * MIN_INTERVAL)
    headline = (f"הסופר החברתי: {len(changes)} שינויים בעגלה ({adds} הוספות, {len(changes) - adds} העלאות כמות)"
                f" · סה״כ עגלה {_fmt(cart['total'])}→≈{_fmt(expected)}")
    summary = headline + f" · רץ ברקע ~{eta} שנ׳ אחרי האישור; לא מוריד ולא מסיר כלום; בלי הזמנה/תשלום"
    payload = {"run_id": run_id, "changes": changes, "total_before": cart["total"], "expected_total": expected,
               "summary": summary}
    ident = _approvals().create(CART_BATCH_ACTION, payload)
    detail = [now_txt] + [f"• {c['name']} {_fmt(c['unit_price'])} · {c['qty_before']}→{c['qty_after']}" for c in changes]
    if skipped:
        detail.append("לא בהצעה: " + " · ".join(f"{x['name']} ({x['reason']})" for x in skipped))
    return _result("pending_approval", "\n".join([headline] + detail), approval_id=ident, headline=headline,
                   cart=_public_cart(cart), changes=[{k: v for k, v in c.items() if k != "cart_id"} for c in changes],
                   skipped=skipped, expected_total=expected, eta_seconds=eta, cart_url=CART_URL)


def on_batch(listener):
    if listener not in _batch_listeners:
        _batch_listeners.append(listener)


def _publish(run_id, result):
    for fn in list(_batch_listeners):
        try:
            fn(run_id, dict(result))
        except Exception:
            pass


def _batch_text(result):
    w, f, n = result["written"], result["failed"], result["not_sent"]
    total = len(w) + len(f) + len(n) + len(result["changed"])
    head = {"done": "✅ העגלה בסופר החברתי עודכנה", "partial": "⚠️ העגלה עודכנה חלקית",
            "failed": "❌ העגלה לא עודכנה", "login_required": "❌ החיבור לסופר החברתי פג — לא שיניתי כלום",
            "unverified": "⚠️ שלחתי עדכונים אבל לא הצלחתי לקרוא שוב את העגלה"}.get(result["status"], result["status"])
    out = [f"{head}: {len(w)}/{total} שורות" + (f" · סה״כ עגלה {_fmt(result.get('total'))}" if result.get("total")
                                               is not None else "")]
    if w:
        out.append("נכתב בעגלה: " + " · ".join(f"{x['name']} ×{x['quantity']}" for x in w[:15])
                   + (f" … ועוד {len(w) - 15}" if len(w) > 15 else ""))
    if f:
        out.append("לא עודכנו: " + " · ".join(f"{x['name']} ({x['reason']})" for x in f))
    if result["changed"]:
        out.append("השתנו באתר מאז ההצעה (לא נגעתי): " + " · ".join(x["name"] for x in result["changed"]))
    if n:
        out.append("לא נשלחו: " + " · ".join(x["name"] for x in n))
    out.append(f"העגלה: {CART_URL} (התשלום אצלך בלבד)")
    return "\n".join(out)


def _run_batch(ident, payload):
    changes = payload.get("changes") or []
    run_id = payload.get("run_id")
    res = {"batch_id": ident, "status": "running", "written": [], "failed": [], "not_sent": [], "changed": [],
           "total": None, "cart_url": CART_URL}
    _batches[ident] = res
    attempted = []
    with _lock:
        try:
            try:
                _require_login(fresh=True)
                cart = _read_cart()
            except LoginRequired:
                res.update(status="login_required", not_sent=[{"name": c["name"]} for c in changes])
                return
            except SiteError as exc:
                res.update(status="failed", not_sent=[{"name": c["name"]} for c in changes], error=str(exc)[:200])
                return
            for k, c in enumerate(changes):
                pid = c["product_id"]
                line = _line(cart, pid)
                now_qty = line["quantity"] if line else 0
                if now_qty != c["qty_before"]:
                    res["changed"].append({"name": c["name"], "now": now_qty})
                    continue
                if line is None:
                    url, form = CART_ADD_URL, {"product_id": pid, "quantity": str(c["qty_after"])}
                else:
                    url, form = CART_EDIT_URL, {"data[0][key]": line["cart_id"], "data[0][quantity]": str(c["qty_after"])}
                try:
                    body = _cart_send("POST", url, form)
                    problem = _check_write_response("add" if url == CART_ADD_URL else "set_qty", body, c["qty_after"])
                    attempted.append((c, problem))
                except (SiteBlocked, LoginRequired) as exc:     # stop: no more requests
                    res["failed"].append({"name": c["name"], "reason": _clip(str(exc), 80)})
                    res["not_sent"] = [{"name": x["name"]} for x in changes[k + 1:]]
                    res["status"] = "failed" if not attempted else "partial"
                    res["error"] = str(exc)[:200]
                    break
                except SiteError as exc:                       # may or may not have reached the site: verify
                    attempted.append((c, str(exc)[:80]))
            if res["status"] != "running":                     # blocked mid-batch: no re-read is allowed now
                res["failed"] += [{"name": c["name"], "reason": "נשלח, לא אומת (האתר חסם)"} for c, _p in attempted]
                res["status"] = "unverified" if attempted else "failed"
                return
            try:
                final = _read_cart()
            except SiteError as exc:
                res.update(status="unverified", error=str(exc)[:200])
                res["failed"] += [{"name": c["name"], "reason": "לא אומת"} for c, _p in attempted]
                return
            res["total"] = final["total"]
            for c, problem in attempted:
                got = _line(final, c["product_id"])
                got_qty = got["quantity"] if got else 0
                if got_qty == c["qty_after"]:
                    res["written"].append({"name": c["name"], "quantity": got_qty})
                else:
                    res["failed"].append({"name": c["name"], "reason": f"בעגלה {got_qty} במקום {c['qty_after']}"
                                          + (f"; {problem}" if problem else "")})
            if res["status"] == "running":
                res["status"] = ("done" if res["written"] and not res["failed"] and not res["changed"]
                                 else "partial" if res["written"] else "failed")
        finally:
            if res["status"] == "running":
                res["status"] = "failed"
            res["text"] = _batch_text(res)
            _batches[ident] = res
            if payload.get("_approval_id"):        # → Telegram: Hermes pushes this text under the prompt
                try:
                    _approvals().finish_background(payload["_approval_id"], res["status"] == "done", res["text"])
                except Exception:
                    pass
            _publish(run_id, res)


def apply_batch(payload, approval_id=None):
    """Approved batch (a background approval action): validates and starts the writes in the background (one
    POST per line, never retried, re-read and verified at the end). The result goes to the grocery run
    (grocery_cart_status) AND to the approval (approvals.finish_background), which Hermes pushes to Telegram."""
    p = dict(payload) if isinstance(payload, dict) else {}
    if approval_id:
        p["_approval_id"] = str(approval_id)
    changes = p.get("changes")
    if not isinstance(changes, list) or not changes or len(changes) > MAX_BATCH:
        raise ValueError("הצעת עגלה לא תקינה")
    for c in changes:
        _pid(c.get("product_id"))
        if c.get("op") not in ("add", "set_qty") or _batch_qty(c.get("qty_after")) is None \
                or not isinstance(c.get("qty_before"), int) or c["qty_before"] >= c["qty_after"]:
            raise ValueError("הצעת עגלה לא תקינה")
    ident = os.urandom(6).hex()
    _batches[ident] = {"batch_id": ident, "status": "running"}
    _publish(p.get("run_id"), {"batch_id": ident, "status": "running", "changes": len(changes)})
    threading.Thread(target=_run_batch, args=(ident, p), daemon=True).start()
    eta = int((len(changes) + 3) * MIN_INTERVAL)
    return {"status": "started", "batch_id": ident, "changes": len(changes), "eta_seconds": eta,
            "text": f"מאושר, מעדכן {len(changes)} שורות בעגלה ברקע (~{eta} שנ׳). התוצאה תישלח לכאן כשיסתיים."}


def batch_status(ident):
    return _batches.get(str(ident)) or {"status": "unknown"}


def register_actions():
    _approvals().register_action(CART_ACTION, apply_write)
    _approvals().register_action(CART_BATCH_ACTION, apply_batch, background=True)


def register_routes(router):
    router.post("/v1/super-hist/login/start", start_login)
    router.post("/v1/super-hist/login/code", submit_code)
    router.get("/v1/super-hist/status", status)
    router.post("/v1/super-hist/logout", logout)
    router.get("/v1/super-hist/cart", cart_view)
    router.post("/v1/super-hist/search", search)
    router.post("/v1/proposals/super-hist-cart/add", propose_add, 202)
    router.post("/v1/proposals/super-hist-cart/set-qty", propose_set_qty, 202)
    router.post("/v1/proposals/super-hist-cart/remove", propose_remove, 202)
