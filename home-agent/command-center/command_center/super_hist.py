"""Histadrut "הסופר החברתי" (super-hist.co.il) login with a one-time SMS code.

The session lives HERE (server-side cookie jar), never in Hermes's browser: the site's login is two plain jQuery
AJAX posts (``account/login/sms`` with the ID number, then ``account/login/sms_validate`` with the 6-digit code),
so no browser is needed and a multi-minute wait for Ben to paste the code costs nothing.

Privacy: the ID number is read from a root-only secret file and is never returned, logged, or sent to Hermes.
The code is used once and never stored or logged. Only status/metadata leaves this module.
Politeness: at most one request to the site every MIN_INTERVAL seconds; no automatic resend or retry.
"""

import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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

_lock = threading.Lock()
_state = {"phase": "idle", "sent_at": 0.0, "attempts": 0, "logged_in_at": 0.0}
_last_request = [0.0]
_jar = None
_sleep = time.sleep          # tests replace these two
_now = time.time


class SiteError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


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


def _send(method, url, form=None, ajax=False, follow=True):
    """One HTTP request with the shared cookie jar. Returns (status, headers, text). Never raises on HTTP status."""
    _throttle()
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {"User-Agent": UA, "Accept-Language": "he-IL,he;q=0.9"}
    if ajax:
        headers.update({"X-Requested-With": "XMLHttpRequest", "Referer": LOGIN_PAGE, "Origin": BASE,
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    handlers = [urllib.request.HTTPCookieProcessor(_cookie_jar())]
    if not follow:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read(3_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(200_000).decode("utf-8", "replace")
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


def start_login(_payload=None, _query=None):
    """Ask the site to SMS a one-time code to the phone(s) on the membership. Never resends automatically."""
    with _lock:
        vat_id = _read_vat_id()
        if not vat_id:
            return _result("not_configured", "חסר מספר תעודת זהות בהגדרות השרת (קובץ סודי). צריך להגדיר אותו פעם אחת בשרת.")
        now = _now()
        if _state["phase"] == "code_sent" and now - _state["sent_at"] < RESEND_COOLDOWN:
            left = int(RESEND_COOLDOWN - (now - _state["sent_at"]))
            return _result("cooldown", f"קוד כבר נשלח לפני פחות מדקה. אפשר לבקש שוב בעוד {left} שניות.",
                           retry_after_seconds=left)
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
            _state.update(phase="code_sent", sent_at=_now(), attempts=0)
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
    code = re.sub(r"[\s\-]", "", str((payload or {}).get("code", "")))
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
            _state.update(phase="logged_in", logged_in_at=_now(), attempts=0)
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
            _state["phase"] = "logged_in"
            _save_cookies()
        elif _state["phase"] == "logged_in":
            _state["phase"] = "idle"
        text = ("מחובר לסופר החברתי." if logged_in else
                "ממתין לקוד מ-SMS." if pending else "לא מחובר לסופר החברתי.")
        return _result("logged_in" if logged_in else ("code_sent" if pending else "logged_out"), text,
                       configured=configured, pending_code=pending)


def logout(_payload=None, _query=None):
    """Forget the local session (cookie jar). Does not contact the site."""
    with _lock:
        _cookie_jar().clear()
        try:
            COOKIES_FILE.unlink()
        except FileNotFoundError:
            pass
        _state.update(phase="idle", sent_at=0.0, attempts=0, logged_in_at=0.0)
        return _result("logged_out", "ניתקתי את החיבור השמור לסופר החברתי.")


def register_routes(router):
    router.post("/v1/super-hist/login/start", start_login)
    router.post("/v1/super-hist/login/code", submit_code)
    router.get("/v1/super-hist/status", status)
    router.post("/v1/super-hist/logout", logout)
