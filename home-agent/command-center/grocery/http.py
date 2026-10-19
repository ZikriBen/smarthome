"""Tiny HTTP layer: `requests` if installed, else urllib. Polite, no bypass of anything.

A *transport* is a callable  (method, url, headers, body_bytes|None, timeout) -> Response.
Tests inject a fake transport; nothing in the test suite touches the network.
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
CLOUDFLARE_MSG = ("Cloudflare blocked — run from an Israeli home IP "
                  "(e.g. the NUC). This tool does not try to bypass Cloudflare.")

SENSITIVE_HEADERS = {"ecomtoken", "authorization", "cookie", "set-cookie", "x-auth-token", "csrftoken"}


@dataclass
class Response:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    text: str = ""

    def header(self, name: str) -> str:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return ""

    @property
    def looks_html(self) -> bool:
        t = self.text.lstrip()[:200].lower()
        return "text/html" in self.header("content-type").lower() or t.startswith("<!doctype") or t.startswith("<html")

    def json(self):
        return json.loads(self.text)


class HttpError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class CloudflareBlocked(HttpError):
    pass


class SessionExpired(HttpError):
    pass


class RateLimited(CloudflareBlocked):
    """HTTP 429: stop at once (exit 10). Never retried — retrying would be routing around the site's limit."""
    pass


# Markers of a bot-protection challenge / captcha WALL (not a normal page: Shufersal's normal pages contain
# 'recaptcha' config, so a bare 'captcha' substring is NOT a signal). Lower-case, checked on the first 5 kB.
CHALLENGE_MARKERS = ("cf-chl", "challenge-platform", "/cdn-cgi/challenge", "just a moment...", "attention required!",
                     "awswaf", "aws-waf-token", "_incapsula_resource", "captcha-delivery", "px-captcha")
CHALLENGE_HEADERS = ("cf-mitigated", "x-amzn-waf-action")


def is_challenge(resp: "Response") -> bool:
    if any(h.lower() in CHALLENGE_HEADERS for h in resp.headers):
        return True
    if not resp.looks_html:
        return False
    t = resp.text[:5000].lower()
    return any(m in t for m in CHALLENGE_MARKERS)


class RobotsDisallowed(HttpError):
    """The site's robots.txt disallows this path. We never work around it."""
    pass


class RobotsPolicy:
    """Minimal robots.txt reader for the `User-agent: *` group: Disallow/Allow with `*` and `$` wildcards
    (longest match wins, Allow wins ties), Crawl-delay, Request-rate, Visit-time.
    The stdlib urllib.robotparser ignores wildcards, which Shufersal's file uses (`*?query=*`)."""

    def __init__(self, text: str, source: str = ""):
        self.source = source
        self.rules = []                 # (allow: bool, pattern)
        self.crawl_delay: Optional[float] = None
        self.request_rate: Optional[str] = None
        self.visit_time: Optional[str] = None    # "HHMM-HHMM" UTC
        group, in_star = [], False
        last_was_ua = False
        for line in (text or "").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if not last_was_ua:
                    group = []
                group.append(v)
                in_star = "*" in group
                last_was_ua = True
                continue
            last_was_ua = False
            if not in_star:
                continue
            if k in ("disallow", "allow") and v:
                self.rules.append((k == "allow", v))
            elif k == "crawl-delay":
                try:
                    self.crawl_delay = float(v)
                except ValueError:
                    pass
            elif k == "request-rate":
                self.request_rate = v
                m = re.match(r"(\d+)\s*/\s*(\d+)", v)
                if m and int(m.group(1)) > 0:     # "1/10" = 1 page per 10 s
                    self.crawl_delay = max(self.crawl_delay or 0, int(m.group(2)) / int(m.group(1)))
            elif k == "visit-time":
                self.visit_time = v

    @staticmethod
    def _match(pattern: str, path: str) -> bool:
        rx = "".join(".*" if ch == "*" else ("$" if ch == "$" and i == len(pattern) - 1 else re.escape(ch))
                     for i, ch in enumerate(pattern))
        return re.match(rx, path) is not None

    def allowed(self, url: str) -> bool:
        u = urllib.parse.urlsplit(url)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        best = None   # (len, allow)
        for allow, pat in self.rules:
            if self._match(pat, path):
                cand = (len(pat), allow)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                    best = cand
        return True if best is None else best[1]

    def in_visit_time(self, now_utc) -> Optional[bool]:
        """None if the file has no Visit-time; else whether now (UTC datetime) is inside the window."""
        m = re.match(r"(\d{2})(\d{2})\s*-\s*(\d{2})(\d{2})", self.visit_time or "")
        if not m:
            return None
        a = int(m.group(1)) * 60 + int(m.group(2))
        b = int(m.group(3)) * 60 + int(m.group(4))
        t = now_utc.hour * 60 + now_utc.minute
        return a <= t <= b if a <= b else (t >= a or t <= b)


def redact(value: str, keep: int = 4) -> str:
    if not value:
        return value
    v = str(value)
    return (v[:keep] + "…[redacted]") if len(v) > keep * 3 else "[redacted]"


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {k: (redact(v) if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def _requests_transport(method, url, headers, body, timeout):
    import requests  # type: ignore
    r = requests.request(method, url, headers=headers, data=body, timeout=timeout)
    r.encoding = r.encoding or "utf-8"
    return Response(r.status_code, dict(r.headers), r.text)


def _urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return Response(resp.status, dict(resp.headers), raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        return Response(e.code, dict(e.headers or {}), raw.decode("utf-8", "replace"))


def default_transport() -> Callable:
    try:
        import requests  # noqa: F401
        return _requests_transport
    except ImportError:
        return _urllib_transport


class Client:
    """Sequential, delayed, retrying JSON client."""

    def __init__(self, transport: Optional[Callable] = None, delay: float = 1.5,
                 retries: int = 3, backoff: float = 2.0, timeout: float = 30.0,
                 sleep: Optional[Callable[[float], None]] = None, log: Callable[[str], None] = None,
                 block_msg: str = CLOUDFLARE_MSG, block_detector: Optional[Callable] = None,
                 crawl_delay: float = 0.0, robots: Optional[RobotsPolicy] = None,
                 robots_mode: str = "enforce", wait_first: bool = False):
        """crawl_delay: hard minimum seconds between requests (e.g. Shufersal robots.txt `Crawl-delay: 10`);
        the effective delay is max(delay, crawl_delay) + 0-0.4 s jitter, and retries wait at least that too.
        robots: a RobotsPolicy. robots_mode="enforce": a disallowed path raises RobotsDisallowed BEFORE anything
        is sent; "note": only logged + recorded in .robots_notes (used for Ben's own-account Shufersal cart, by his
        explicit decision). wait_first: also wait before the FIRST request (a new client right after another one,
        e.g. phase 2 straight after phase 1, still keeps the crawl delay).
        Stop rules (never retried): any 403, 429, captcha/challenge page, HTML where JSON was expected
        -> CloudflareBlocked / RateLimited (CLI exit 10). 5xx / network errors are retried for GET only; a POST is
        never retried (a repeated cart POST could double-add).
        `sleep` is injectable so tests never really wait."""
        self.transport = transport or default_transport()
        self.crawl_delay = max(0.0, float(crawl_delay or 0))
        self.robots = robots
        self.robots_mode = robots_mode
        self.robots_notes = []
        self.wait_first = wait_first
        self.delay = max(float(delay or 0), self.crawl_delay)
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.sleep = sleep or (lambda sec: time.sleep(sec))   # resolved at call time -> patchable in tests
        self.log = log or (lambda m: None)
        self.block_msg = block_msg
        self.block_detector = block_detector   # extra site-specific "this is a block page" check
        self.request_count = 0

    def _polite_wait(self):
        if (self.request_count or self.wait_first) and self.delay > 0:
            self.sleep(self.delay + random.uniform(0, 0.4))

    def request_json(self, method: str, url: str, headers: Dict[str, str],
                     body=None, auth: bool = False, expect: str = "json"):
        """expect='json' (default) parses JSON; expect='text' returns the body (e.g. HTML cart page)."""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        attempt = 0
        while True:
            if self.robots is not None and not self.robots.allowed(url):
                path = urllib.parse.urlsplit(url).path
                if self.robots_mode != "note":
                    raise RobotsDisallowed(f"robots.txt disallows {path} ({self.robots.source}) — not requested; "
                                           "this tool does not work around robots.txt")
                if path not in self.robots_notes:
                    self.robots_notes.append(path)
                    self.log(f"note: robots.txt disallows {path} for crawlers; requested anyway for Ben's own "
                             "logged-in cart (his explicit decision; no checkout)")
            self._polite_wait()
            self.request_count += 1
            self.log(f"{method} {url} headers={redact_headers(headers)}")
            try:
                resp = self.transport(method, url, headers, data, self.timeout)
            except (HttpError, AssertionError):   # AssertionError: the test suite's no-network guard
                raise
            except Exception as e:      # timeouts / DNS / connection reset: retry GET only, then a clean HttpError
                if method == "GET" and attempt < self.retries:
                    wait = max(self.backoff * (2 ** attempt), self.crawl_delay)
                    self.log(f"network error {type(e).__name__}; retry {attempt + 1}/{self.retries} in {wait:.1f}s")
                    self.sleep(min(wait, 60))
                    attempt += 1
                    continue
                raise HttpError(f"network error from {urllib.parse.urlsplit(url).netloc}: {type(e).__name__}")
            host = urllib.parse.urlsplit(url).netloc
            # ---- stop rules first: NEVER retried, whatever the method ----
            if resp.status == 429:
                raise RateLimited(f"HTTP 429 (rate limited) from {host} — stopped immediately, not retried. "
                                  "Wait (hours) before running again; do not lower the delay.", 429)
            if (self.block_detector and self.block_detector(resp)) or is_challenge(resp):
                raise CloudflareBlocked(self.block_msg + f" [HTTP {resp.status} challenge/block page from {host}; "
                                        "stopped, not retried]", resp.status)
            if resp.status == 403:
                why = self.block_msg if resp.looks_html else (
                    "the site refused the request (WAF block or rejected session) — re-capture the session in a "
                    "browser if it persists")
                raise CloudflareBlocked(f"HTTP 403 from {host}: {why} [stopped, not retried]", 403)
            if resp.status in (500, 502, 503, 504) and method == "GET" and attempt < self.retries:
                wait = max(self.backoff * (2 ** attempt), self.crawl_delay)
                self.log(f"HTTP {resp.status}; retry {attempt + 1}/{self.retries} in {wait:.1f}s")
                self.sleep(min(wait, 60))
                attempt += 1
                continue
            if resp.status == 401:
                if auth:
                    raise SessionExpired(
                        f"HTTP {resp.status} from {host}: session rejected — re-login in a browser "
                        "and recapture the session file.", resp.status)
                raise HttpError(f"HTTP {resp.status} from {url}", resp.status)
            if resp.status != 200:
                note = " (POST: not retried — check the cart before running again)" if method != "GET" else ""
                raise HttpError(f"HTTP {resp.status} from {url}{note}: {resp.text[:160]!r}", resp.status)
            if expect == "text":
                return resp.text
            if resp.looks_html:
                # 200 with an HTML body where JSON was expected = challenge page
                raise CloudflareBlocked(self.block_msg + " [HTML instead of JSON; stopped, not retried]", resp.status)
            try:
                return resp.json()
            except ValueError:
                raise HttpError(f"non-JSON response from {url}: {resp.text[:160]!r}", resp.status)



class FixtureSearcher:
    """Offline searcher: <dir>/index.json maps search term -> file with a saved search response.
    `results_key` = "data" (Rami Levy) or "results" (Shufersal)."""

    def __init__(self, directory: str, log: Callable[[str], None] = None, results_key: str = "data"):
        import os
        self.results_key = results_key
        self.dir = directory
        self.log = log or (lambda m: None)
        idx_path = os.path.join(directory, "index.json")
        with open(idx_path, encoding="utf-8") as f:
            self.index = json.load(f)
        self.request_count = 0

    def search(self, term: str, size: Optional[int] = None):
        import os
        self.request_count += 1
        fn = self.index.get(term) if not term.startswith("_") else None
        if not fn:
            self.log(f"[fixtures] no fixture for term {term!r} -> 0 results")
            return []
        with open(os.path.join(self.dir, fn), encoding="utf-8") as f:
            return (json.load(f) or {}).get(self.results_key) or []
