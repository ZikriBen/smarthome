"""הסופר החברתי cart tools, offline: SYNTHETIC anonymized fixtures + a stateful fake site (no network).

Covers parsing (cart incl. empty, mini-cart, search incl. specials/out_of_stock/weight), the URL policy in
_send, 403/429/challenge stop, one automatic login (mocked), proposals that require approval, the
in_cart -> set_qty path, the approved write with re-read/verify and partial failure, and the throttle.
"""
import http.server
import json
import os
import re
import sys
import tempfile
import threading
import types
import unittest
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from command_center import super_hist as sh  # noqa: E402
from command_center.router import Router  # noqa: E402
from test_approvals import load_approvals  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "super-hist"
CART_FULL = (FX / "cart_full.html").read_text()
CART_EMPTY = (FX / "cart_empty.html").read_text()
INFO_EMPTY = (FX / "cart_info_empty.html").read_text()
INFO_3 = (FX / "cart_info_3.html").read_text()
SEARCH = (FX / "search_snacks.json").read_text()
REAL_BASE = sh.BASE


def render_cart(lines):
    """The real line/totals markup (as in cart_full.html: self-closing <img />, the price repeated for mobile),
    generated from the fake site's state."""
    rows = []
    for ln in lines:
        pid, cid = ln["pid"], ln["cid"]
        rows.append(f'<div class="cart_product product-item py-4" data-product_id="{pid}"   data-cart-id="{cid}"  >'
                    f'<div class="product_row"><div class="grid"><div class="remove_item">'
                    f'<i class="fal fa-times" onclick="cart.remove(\'{cid}\', \'{pid}\');"></i></div>'
                    f'<div class="product-thumb"><a href="#"><img src="x.jpg" alt="" class="img-fluid" /></a></div>'
                    f'<div class="md:col-span-2 product-description px-2"><a href="#">{ln["name"]}</a></div>'
                    f'<div class="product-price"><div class="flex price_span"><del>₪{ln["price"] + 1:.2f}</del>'
                    f'<ins>₪{ln["price"]:.2f}</ins></div></div>'
                    f'<div class="qty_column"><div class="quantity buttons_added"><i data-action="minus"></i>'
                    f'<input type="number" class="quantity_field form-control"  name="quantity" value="{ln["qty"]}">'
                    f'<i data-action="plus"></i></div><div class="flex md:hidden price_span"><del>₪{ln["price"] + 1:.2f}</del>'
                    f'<ins>₪{ln["price"]:.2f}</ins></div></div></div></div></div>')
    total = sum(ln["price"] * ln["qty"] for ln in lines)
    totals = (f'<div class="total_sum flex sub_total "><span class="total_title">סכום</span>'
              f'<span class="total_text" dir="ltr">₪{total:.2f}</span></div>'
              f'<div class="total_sum flex final_sum total "><span class="total_title">סה"כ</span>'
              f'<span class="total_text" dir="ltr">₪{total:.2f}</span></div>'
              if lines else '<p class="empty-cart">עגלת הקניות שלך ריקה!</p>')
    return f'<div id="checkout-cart"><div class="cart-items">{"".join(rows)}</div>{totals}</div>'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (sh.DATA_DIR, sh.VAT_ID_FILE, sh.COOKIES_FILE, sh.INBOX_DB, sh._now, sh._sleep, sh._approvals)
        sh.DATA_DIR, sh.VAT_ID_FILE, sh.COOKIES_FILE = root, root / "vat_id", root / "cookies.txt"
        sh.INBOX_DB = root / "no-inbox" / "codes.sqlite"
        sh.COOKIES_FILE.write_text("# Netscape HTTP Cookie File\n")  # a saved session exists
        sh._jar = None
        sh._state.update(phase="logged_in", sent_at=0.0, started_at=0.0, attempts=0, logged_in_at=0.0, checked_at=0.0)
        sh._catalog.clear()
        sh._blocked_until[0] = 0.0
        self.clock = [2_000_000.0]
        self.slept = []
        sh._now = lambda: self.clock[0]
        sh._sleep = lambda s: (self.slept.append(s), self.clock.__setitem__(0, self.clock[0] + s))
        sh._last_request[0] = 0.0
        self.ap, self.db = load_approvals()
        sh._approvals = lambda: self.ap
        sh.register_actions()

    def tearDown(self):
        (sh.DATA_DIR, sh.VAT_ID_FILE, sh.COOKIES_FILE, sh.INBOX_DB, sh._now, sh._sleep, sh._approvals) = self.saved
        self.db.close()
        sh._jar = None
        sh._catalog.clear()
        sh._blocked_until[0] = 0.0
        self.tmp.cleanup()


class FakeSite(Base):
    """A stateful fake site behind sh._send. The real URL policy and throttle still run first."""

    def setUp(self):
        super().setUp()
        self.real_send = sh._send
        sh._send = self.fake_send
        self.calls = []
        self.logged_in = True
        self.lines = [{"pid": "900101", "cid": "7001", "name": "חלב טרי 3% 1 ליטר", "price": 6.20, "qty": 2}]
        self.override = {}     # route -> (status, headers, body) one-shot
        self.raise_on = {}     # route -> exception, one-shot
        self.next_cid = 7100

    def tearDown(self):
        sh._send = self.real_send
        super().tearDown()

    def fake_send(self, method, url, form=None, ajax=False, follow=True, referer=None):
        sh.check_url(method, url)
        sh._throttle()
        u = urllib.parse.urlsplit(url)
        route = sh._route(u.query) or u.path
        self.calls.append((method, route, dict(form or {}), ajax, follow))
        if route in self.raise_on:
            exc = self.raise_on.pop(route)
            if route == "checkout/cart/add":   # the POST reached the site, then the connection dropped
                self._add(form)
            raise exc
        if route in self.override:
            return self.override.pop(route)
        if route == "account/account":
            return (200, {}, "<html>החשבון שלי</html>") if self.logged_in else (302, {"Location": "/login/"}, "")
        if not self.logged_in and route != "/search-results/":
            return 200, {}, render_cart([])   # an expired session sees an empty guest cart
        if route == "checkout/cart":
            return 200, {}, render_cart(self.lines)
        if route == "common/cart/info":
            total = sum(ln["price"] * ln["qty"] for ln in self.lines)
            units = sum(ln["qty"] for ln in self.lines)
            return 200, {}, (f'<div class="cart-items"><input type="hidden" class="hidden_total" '
                             f'value="{units} פריט\\ים - ₪{total:.2f}"><input type="hidden" '
                             f'class="hidden_total_for_small_cart" value="{units}"></div>')
        if route == "/search-results/":
            return 200, {"Content-Type": "text/html"}, SEARCH
        if route == "checkout/cart/add":
            ln = self._add(form)
            return 200, {}, json.dumps({"success": "נוסף", "cart_id": ln["cid"], "total": "x",
                                        "current_product_quantity": ln["qty"]})
        if route == "checkout/cart/remove":
            self.lines = [ln for ln in self.lines if ln["cid"] != form["key"]]
            return 200, {}, json.dumps({"total": "x", "countProducts": len(self.lines)})
        if route == "checkout/cart/edit":
            for ln in self.lines:
                if ln["cid"] == form["data[0][key]"]:
                    ln["qty"] = int(form["data[0][quantity]"])
            return 200, {}, json.dumps({"success": True})
        return 404, {}, ""

    def _add(self, form):
        for ln in self.lines:
            if ln["pid"] == form["product_id"]:
                ln["qty"] += int(form["quantity"])   # the site INCREASES an existing line
                return ln
        self.next_cid += 1
        ln = {"pid": form["product_id"], "cid": str(self.next_cid), "name": "חטיף", "price": 5.90,
              "qty": int(form["quantity"])}
        self.lines.append(ln)
        return ln

    def routes(self):
        return [(m, r) for m, r, *_ in self.calls]

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def decide(self, ident):
        return self.ap.decide(ident, "approve", via="chat", actor="ben")

    # ---- the automatic login, mocked: no SMS, no site, fake clock
    def mock_login(self, start_status="code_sent", relay_after=None, relay_status="logged_in"):
        """start_login → start_status (code_sent sets the pending phase); the relay delivers a code after
        `relay_after` polls (None = never)."""
        self.logged_in = False
        self.starts, self.relay_polls = [], []
        saved = sh.start_login, sh._auto_submit, sh._inbox_enabled
        self.addCleanup(lambda: setattr(sh, "start_login", saved[0]) or setattr(sh, "_auto_submit", saved[1])
                        or setattr(sh, "_inbox_enabled", saved[2]))

        def start(payload=None, _q=None):
            self.starts.append(dict(payload or {}))
            if start_status == "code_sent":
                sh._state.update(phase="code_sent", sent_at=sh._now(), started_at=sh._now())
            if start_status == "logged_in":
                self.logged_in = True
                sh._state.update(phase="logged_in", logged_in_at=sh._now())
            return {"status": start_status, "text": "x"}

        def relay(seconds):
            self.relay_polls.append(seconds)
            if relay_after is None or len(self.relay_polls) < relay_after:
                return None
            if relay_status == "logged_in":
                self.logged_in = True
                sh._state.update(phase="logged_in", logged_in_at=sh._now())
            return {"status": relay_status, "text": "x"}
        sh.start_login, sh._auto_submit, sh._inbox_enabled = start, relay, (lambda: True)


# --------------------------------------------------------------------------- parsing

class Parsing(unittest.TestCase):
    def test_full_cart(self):
        cart = sh.parse_cart(CART_FULL)
        self.assertEqual(cart["count"], 3)
        milk, bread, tomato = cart["lines"]
        self.assertEqual((milk["product_id"], milk["cart_id"], milk["quantity"]), ("900101", "7001", 2))
        self.assertEqual((milk["unit_price"], milk["shelf_price"]), (6.20, 7.10))   # <ins> sale wins over <del>
        self.assertIn("מחלבה לדוגמה", milk["name"])
        self.assertEqual((bread["unit_price"], bread["quantity"], bread["weighted"]), (12.90, 1, False))
        self.assertEqual((tomato["quantity"], tomato["weighted"]), (0.5, True))
        self.assertIn('"שרי"', tomato["name"])                                        # entities unescaped
        self.assertEqual((cart["subtotal"], cart["total"]), (30.25, 30.25))

    def test_self_closing_tags_do_not_close_a_line(self):
        # Regression from the live run: <img ... /> made html.parser report </img>, which unwound the stack
        # and closed every line before its quantity was read (all 22 real lines failed closed).
        page = ('<div class="cart_product" data-product_id="1" data-cart-id="2"><a><img src="x" /></a><br/>'
                '</span><div class="product-description"><a>שם</a></div><div class="price_span"><ins>₪3.00</ins></div>'
                '<input type="number" name="quantity" value="4"></div>')
        line = sh.parse_cart(page)["lines"][0]
        self.assertEqual((line["name"], line["quantity"], line["unit_price"]), ("שם", 4, 3.0))

    def test_unit_price_times_qty_is_the_total(self):
        cart = sh.parse_cart(CART_FULL)   # the real site shows the UNIT price per line (verified live)
        self.assertAlmostEqual(sum(l["unit_price"] * l["quantity"] for l in cart["lines"]), cart["total"])

    def test_empty_cart_and_mini_cart(self):
        cart = sh.parse_cart(CART_EMPTY)
        self.assertEqual((cart["count"], cart["lines"], cart["total"]), (0, [], None))
        self.assertEqual(sh.parse_cart_info(INFO_EMPTY), (0, 0.0))
        self.assertEqual(sh.parse_cart_info(INFO_3), (3, 30.25))   # hidden <input value=…>
        self.assertEqual(sh.parse_cart_info('<span class="hidden_total">2 פריטים - ₪4.00</span>'), (2, 4.0))

    def test_unknown_page_and_broken_lines_fail_closed(self):
        with self.assertRaises(sh.SiteChanged):
            sh.parse_cart("<html><body>שלום</body></html>")
        with self.assertRaises(sh.SiteChanged):
            sh.parse_cart('<div class="cart_product" data-product_id="1"><input name="quantity" value="1"></div>')
        with self.assertRaises(sh.SiteChanged):
            sh.parse_cart_info("<div>nothing</div>")

    def test_search_json(self):
        found = sh.parse_search(SEARCH)
        self.assertEqual(found["results"], 46)
        by = {p["product_id"]: p for p in found["products"]}
        chips = by["900404"]
        self.assertEqual((chips["price"], chips["shelf_price"], chips["on_sale"]), (5.90, 7.90, True))
        self.assertEqual(chips["promo"], "2 ב-10")
        self.assertTrue(by["900101"]["in_cart"])
        self.assertTrue(by["900505"]["out_of_stock"])
        self.assertTrue(by["900303"]["weighted"])
        self.assertEqual((by["900606"]["price"], by["900606"]["on_sale"]), (8.40, False))
        self.assertIsNone(by["900606"]["promo"])            # bgfnribbon_text: false -> no promo (not "False")
        self.assertFalse(by["900404"]["weighted"])          # weight: false (bool) on packaged products
        self.assertTrue(all(p["url"].startswith("https://super-hist.co.il/") for p in found["products"]))
        self.assertEqual(sh.parse_search((FX / "search_empty.json").read_text())["products"], [])
        with self.assertRaises(sh.SiteChanged):
            sh.parse_search("<html>not json</html>")

    def test_write_responses(self):
        self.assertIsNone(sh._check_write_response("add", (FX / "cart_add_ok.json").read_text(), 1))
        self.assertIn("2", sh._check_write_response("add", (FX / "cart_add_ok.json").read_text(), 2))
        self.assertIsNone(sh._check_write_response("remove", (FX / "cart_remove_ok.json").read_text(), 0))
        self.assertIsNone(sh._check_write_response("set_qty", (FX / "cart_edit_ok.json").read_text(), 3))
        self.assertIsNotNone(sh._check_write_response("set_qty", '{"error": "כמות לא חוקית"}', 3))
        self.assertIsNotNone(sh._check_write_response("add", "<html>", 1))


class FixturePrivacy(unittest.TestCase):
    def test_fixtures_hold_no_personal_data(self):
        for path in FX.iterdir():
            text = path.read_text(errors="replace")
            self.assertIsNone(re.search(r"(?<!\d)\d{9}(?!\d)", text), path.name)              # ID-like
            self.assertIsNone(re.search(r"(?<!\d)05\d[- ]?\d{7}(?!\d)", text), path.name)     # mobile
            self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), path.name)         # e-mail
            self.assertIsNone(re.search(r"PHPSESSID|Set-Cookie|cf_clearance|__cf_bm", text, re.I), path.name)
            if path.name != "login_forms.html":
                self.assertIn("SYNTHETIC", text, path.name)


# --------------------------------------------------------------------------- URL policy

class Policy(unittest.TestCase):
    ALLOWED = [("GET", "/index.php?route=checkout/cart"), ("GET", "/index.php?route=common/cart/info"),
               ("GET", "/index.php?route=account/account"), ("GET", "/login/"), ("GET", "/"),
               ("POST", "/search-results/"), ("POST", "/index.php?route=checkout/cart/add"),
               ("POST", "/index.php?route=checkout/cart/remove"), ("POST", "/index.php?route=checkout/cart/edit"),
               ("POST", "/index.php?route=account/login/sms"), ("POST", "/index.php?route=account/login/sms_validate")]
    BLOCKED = [("GET", "/index.php?route=checkout/checkout"), ("GET", "/checkout/"),
               ("POST", "/index.php?route=checkout/cart/clear"), ("GET", "/index.php?route=checkout/cart/clear"),
               ("POST", "/index.php?route=extension/total/coupon/coupon"),
               ("POST", "/index.php?route=extension/total/voucher/voucher"),
               ("GET", "/index.php?route=checkout/payment_method"), ("GET", "/index.php?route=account/order"),
               ("GET", "/index.php?route=checkout/confirm"), ("GET", "/index.php?route=checkout/shipping_method"),
               ("GET", "/index.php?route=checkout/success"), ("GET", "/index.php?route=checkout/cart&next=payment"),
               ("GET", "/index.php?route=%63heckout/checkout"), ("GET", "/index.php?route=account/register"),
               ("POST", "/index.php?route=checkout/cart"), ("GET", "/index.php?route=checkout/cart/add"),
               ("GET", "/index.php?route=product/product&product_id=1"), ("DELETE", "/index.php?route=checkout/cart"),
               ("GET", "/search-results/?x=1"), ("POST", "/index.php?route=checkout/cart/add&route=checkout/checkout")]

    def test_allowlist(self):
        for method, path in self.ALLOWED:
            sh.check_url(method, REAL_BASE + path)

    def test_blocklist(self):
        for method, path in self.BLOCKED:
            with self.assertRaises(sh.PolicyError, msg=f"{method} {path}"):
                sh.check_url(method, REAL_BASE + path)

    def test_other_hosts_and_schemes(self):
        for url in ("http://super-hist.co.il/index.php?route=checkout/cart",
                    "https://evil.example/index.php?route=checkout/cart",
                    "https://super-hist.co.il.evil.example/",
                    "https://user:pw@super-hist.co.il/"):
            with self.assertRaises(sh.PolicyError, msg=url):
                sh.check_url("GET", url)


class PolicyInSend(Base):
    """The real _send refuses forbidden URLs before throttling or touching the network."""

    def test_refused_before_anything(self):
        import socket
        real_connect = socket.socket.connect
        socket.socket.connect = lambda *_a, **_k: self.fail("network touched")
        try:
            for method, path in Policy.BLOCKED:
                with self.assertRaises(sh.PolicyError):
                    sh._send(method, REAL_BASE + path, form={} if method == "POST" else None)
        finally:
            socket.socket.connect = real_connect
        self.assertEqual(self.slept, [])
        self.assertEqual(sh._last_request[0], 0.0)


class RedirectSite(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        RedirectSite.hits.append((self.path, self.headers.get("X-Requested-With")))
        if self.path == "/login/":
            self.send_response(302)
            self.send_header("Location", "/index.php?route=checkout/checkout")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "route=checkout/cart" in self.path:
            raw = render_cart([{"pid": "900101", "cid": "7001", "name": "x", "price": 1.0, "qty": 1}]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class RealHTTPPolicy(Base):
    def setUp(self):
        super().setUp()
        RedirectSite.hits = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectSite)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.saved_urls = (sh.BASE, sh.CART_URL)
        sh.BASE = f"http://127.0.0.1:{self.server.server_port}"
        sh.CART_URL = sh.BASE + "/index.php?route=checkout/cart"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        sh.BASE, sh.CART_URL = self.saved_urls
        super().tearDown()

    def test_redirect_to_checkout_is_not_followed(self):
        with self.assertRaises(sh.PolicyError):
            sh._send("GET", sh.BASE + "/login/")
        self.assertEqual([h[0] for h in RedirectSite.hits], ["/login/"])   # checkout never requested

    def test_cart_get_is_ajax(self):
        body = sh._cart_send("GET", sh.CART_URL)
        self.assertEqual(sh.parse_cart(body)["count"], 1)
        self.assertEqual(RedirectSite.hits[-1][1], "XMLHttpRequest")


# --------------------------------------------------------------------------- read tools

class ReadTools(FakeSite):
    def test_view(self):
        out = sh.cart_view({}, {})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["cart"]["count"], 1)
        self.assertEqual(out["cart"]["total"], 12.40)
        self.assertNotIn("cart_id", json.dumps(out))
        self.assertEqual(self.routes(), [("GET", "account/account"), ("GET", "checkout/cart")])
        self.assertTrue(all(c[3] for c in self.calls if c[1] == "checkout/cart"))   # X-Requested-With

    def test_login_check_is_cached(self):
        sh.cart_view({}, {})
        sh.cart_view({}, {})
        self.assertEqual(self.routes().count(("GET", "account/account")), 1)

    def test_empty_cart_uses_canary(self):
        self.lines = []
        out = sh.cart_view({}, {})
        self.assertEqual((out["status"], out["cart"]["count"]), ("ok", 0))
        self.assertIn(("GET", "common/cart/info"), self.routes())

    def test_canary_mismatch_is_site_changed(self):
        self.override["checkout/cart"] = (200, {}, CART_EMPTY)
        out = sh.cart_view({}, {})
        self.assertEqual(out["status"], "site_changed")

    def test_search(self):
        out = sh.search({"term": "חטיף"}, {})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["results"], 46)
        post = self.posts()[0]
        self.assertEqual(post[2], {"search": "חטיף", "screen_break_points": "desktop"})
        self.assertTrue(post[3])
        self.assertEqual(sh.search({"term": ""}, {})["status"], "invalid")

    def test_requests_are_spaced(self):
        sh.cart_view({}, {})
        sh.search({"term": "חטיף"}, {})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.slept), 2)
        self.assertTrue(all(abs(s - sh.MIN_INTERVAL) < 0.01 for s in self.slept))


class Stops(FakeSite):
    def test_logged_out_auto_login_then_continues(self):
        for fn, payload, ok in ((sh.cart_view, {}, "ok"), (sh.search, {"term": "חטיף"}, "ok"),
                                (sh.propose_add, {"product_id": "900404", "quantity": 2}, "pending_approval")):
            with self.subTest(fn=fn.__name__):
                if fn is sh.propose_add:
                    self.logged_in = True
                    sh.search({"term": "חטיף"}, {})
                self.mock_login(relay_after=2)
                sh._state["checked_at"] = 0.0
                out = fn(payload, {})
                self.assertEqual(out["status"], ok, out)
                self.assertEqual(len(self.starts), 1)                # one SMS
                self.assertEqual(len(self.relay_polls), 2)

    def test_logged_out_login_timeout_one_line_and_stop(self):
        self.mock_login(relay_after=None)
        t0 = sh._now()
        self.calls.clear()
        out = sh.cart_view({}, {})
        self.assertEqual(out["status"], "login_failed")
        self.assertEqual(len(out["text"].splitlines()), 1)
        self.assertIn("3 דק׳", out["text"])
        self.assertEqual(len(self.starts), 1)                        # no retry, no second SMS
        self.assertLessEqual(sh._now() - t0, sh.LOGIN_BUDGET + 2 * sh.MIN_INTERVAL)
        self.assertEqual([r for _m, r in self.routes()], ["account/account"])   # no cart read afterwards

    def test_logged_out_login_failure_no_proposal(self):
        for status in ("rate_limited", "cooldown", "not_configured"):
            with self.subTest(status=status):
                self.mock_login(start_status=status)
                sh._state["checked_at"] = 0.0
                out = sh.propose_add({"product_id": "900404"}, {})
                self.assertEqual(out["status"], "login_failed")
                self.assertEqual(len(self.starts), 1)
                self.assertEqual(self.db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)

    def test_site_still_logged_out_after_login_stops(self):
        self.mock_login(start_status="logged_in")
        real_start = sh.start_login

        def start(payload=None, _q=None):                            # "logged in" but the site disagrees
            out = real_start(payload)
            self.logged_in = False
            return out
        sh.start_login = start
        out = sh.cart_view({}, {})
        self.assertEqual(out["status"], "login_failed")
        self.assertEqual(len(self.starts), 1)                        # exactly one attempt

    def test_real_start_login_without_id_contacts_nothing(self):
        self.logged_in = False                                       # no mock: the real start_login
        self.assertFalse(sh.VAT_ID_FILE.exists())
        out = sh.cart_view({}, {})
        self.assertEqual(out["status"], "login_failed")
        self.assertIn("ת״ז", out["text"])
        self.assertFalse(any("login" in r for _m, r in self.routes()))

    def test_no_saved_session_contacts_nothing(self):
        sh.COOKIES_FILE.unlink()                                     # no ID configured either → no SMS
        self.assertEqual(sh.cart_view({}, {})["status"], "login_failed")
        self.assertEqual(self.calls, [])

    def test_cart_redirect_to_login_logs_in_once(self):
        self.mock_login(relay_after=1)
        self.logged_in = True
        self.override["checkout/cart"] = (302, {"Location": "https://super-hist.co.il/login/"}, "")  # one-shot
        self.assertEqual(sh.cart_view({}, {})["status"], "ok")
        self.assertEqual(len(self.starts), 1)

    def test_403_429_and_challenge_stop_everything(self):
        for reply in ((403, {}, "forbidden"), (429, {}, "slow down"),
                      (200, {}, "<html><title>Just a moment...</title></html>"),
                      (200, {}, '<div class="cf-turnstile" data-sitekey="x"></div>')):
            sh._blocked_until[0] = 0.0
            self.calls.clear()
            self.override["checkout/cart"] = reply
            self.assertEqual(sh.cart_view({}, {})["status"], "site_blocked", reply)
            n = len(self.calls)
            self.assertEqual(sh.cart_view({}, {})["status"], "site_blocked")
            self.assertEqual(sh.search({"term": "x"}, {})["status"], "site_blocked")
            self.assertEqual(len(self.calls), n)   # breaker: nothing more was sent

    def test_blocked_write_post_is_not_retried(self):
        sh.search({"term": "חטיף"}, {})
        ident = sh.propose_add({"product_id": "900404"}, {})["approval_id"]
        self.override["checkout/cart/add"] = (429, {}, "")
        out = self.decide(ident)
        self.assertEqual(out["status"], "failed")
        self.assertIn("ייתכן שהעגלה השתנתה", out["error"])
        self.assertEqual([c[1] for c in self.posts()].count("checkout/cart/add"), 1)


# --------------------------------------------------------------------------- proposals + approved writes

class Writes(FakeSite):
    def test_add_requires_approval_then_executes(self):
        sh.search({"term": "חטיף"}, {})
        out = sh.propose_add({"product_id": "900404", "quantity": 1}, {})
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual([c[1] for c in self.posts()], ["/search-results/"])   # nothing written yet
        summary = self.ap.get(out["approval_id"])["summary"]
        for part in ("הסופר החברתי", "חטיף תפוחי אדמה", "₪5.90", "0→1", "₪12.40", "≈₪18.30"):
            self.assertIn(part, summary)
        result = self.decide(out["approval_id"])
        self.assertEqual(result["status"], "executed", result)
        self.assertEqual((result["result"]["quantity"], result["result"]["total"]), (1, 18.30))
        self.assertEqual([c[1] for c in self.posts()], ["/search-results/", "checkout/cart/add"])
        self.assertEqual(self.posts()[-1][2], {"product_id": "900404", "quantity": "1"})
        tail = self.routes()[-4:]
        self.assertEqual(tail, [("GET", "account/account"), ("GET", "checkout/cart"),
                                ("POST", "checkout/cart/add"), ("GET", "checkout/cart")])
        again = self.decide(out["approval_id"])                        # a second 'מאשר' / tap
        self.assertTrue(again["already_decided"])
        self.assertEqual([c[1] for c in self.posts()].count("checkout/cart/add"), 1)

    def test_deny_writes_nothing(self):
        sh.search({"term": "חטיף"}, {})
        ident = sh.propose_add({"product_id": "900404"}, {})["approval_id"]
        self.assertEqual(self.ap.decide(ident, "deny", via="chat", actor="ben")["status"], "denied")
        self.assertEqual([c[1] for c in self.posts()], ["/search-results/"])

    def test_add_unknown_product_needs_search(self):
        self.assertEqual(sh.propose_add({"product_id": "900404"}, {})["status"], "search_first")

    def test_add_on_existing_line_becomes_set_qty(self):
        sh.search({"term": "חלב"}, {})
        out = sh.propose_add({"product_id": "900101", "quantity": 3}, {})
        self.assertEqual((out["status"], out["proposal"]["op"]), ("pending_approval", "set_qty"))
        self.assertIn("2→3", out["text"])
        self.assertEqual(self.decide(out["approval_id"])["status"], "executed")
        edit = [c for c in self.posts() if c[1] == "checkout/cart/edit"]
        self.assertEqual(edit[0][2], {"data[0][key]": "7001", "data[0][quantity]": "3"})
        self.assertFalse(any(c[1] == "checkout/cart/add" for c in self.posts()))   # never a blind add
        self.assertEqual(self.lines[0]["qty"], 3)

    def test_add_already_enough_proposes_nothing(self):
        out = sh.propose_add({"product_id": "900101", "quantity": 2}, {})
        self.assertEqual(out["status"], "already_in_cart")
        self.assertEqual(self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0)

    def test_refusals(self):
        sh.search({"term": "חטיף"}, {})
        self.assertEqual(sh.propose_add({"product_id": "900505"}, {})["status"], "out_of_stock")
        self.assertEqual(sh.propose_add({"product_id": "900303"}, {})["status"], "weighted_unsupported")
        self.assertEqual(sh.propose_add({"product_id": "900404", "quantity": 99}, {})["status"], "invalid")
        self.assertEqual(sh.propose_add({"product_id": "abc"}, {})["status"], "invalid")
        self.assertEqual(sh.propose_set_qty({"product_id": "900101", "quantity": 0}, {})["status"], "invalid")
        self.assertEqual(sh.propose_set_qty({"product_id": "900404", "quantity": 2}, {})["status"], "not_in_cart")
        self.assertEqual(sh.propose_remove({"product_id": "900404"}, {})["status"], "not_in_cart")
        self.assertEqual(self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0)

    def test_set_qty_and_remove(self):
        out = sh.propose_set_qty({"product_id": "900101", "quantity": 4}, {})
        self.assertEqual(self.decide(out["approval_id"])["status"], "executed")
        self.assertEqual(self.lines[0]["qty"], 4)
        out = sh.propose_remove({"product_id": "900101"}, {})
        self.assertIn("4→0", out["text"])
        self.assertEqual(self.decide(out["approval_id"])["status"], "executed")
        self.assertEqual(self.lines, [])
        self.assertEqual(self.posts()[-1][2], {"key": "7001", "product_id": "900101"})

    def test_cart_changed_since_proposal_writes_nothing(self):
        out = sh.propose_set_qty({"product_id": "900101", "quantity": 4}, {})
        self.lines[0]["qty"] = 5          # Ben changed it in the app meanwhile
        result = self.decide(out["approval_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("העגלה השתנתה מאז ההצעה", result["error"])
        self.assertEqual(self.posts(), [])

    def test_partial_failure_rereads_and_reports(self):
        sh.search({"term": "חטיף"}, {})
        ident = sh.propose_add({"product_id": "900404", "quantity": 1}, {})["approval_id"]
        self.lines[0]["qty"] = 2
        self.raise_on["checkout/cart/add"] = sh.SiteError("timeout")
        # the add reached the site, but say the site also added 1 extra in the app meanwhile -> mismatch path
        orig_add = self._add
        self._add = lambda form: orig_add(dict(form, quantity="2"))
        result = self.decide(ident)
        self.assertEqual(result["status"], "failed")
        self.assertIn("ייתכן שהעגלה השתנתה", result["error"])
        self.assertIn("בפועל 2", result["error"])
        self.assertEqual([c[1] for c in self.posts()].count("checkout/cart/add"), 1)   # never retried
        self.assertEqual(self.routes()[-1], ("GET", "checkout/cart"))                 # re-read after failure

    def test_write_with_expired_session(self):
        out = sh.propose_set_qty({"product_id": "900101", "quantity": 3}, {})
        self.logged_in = False
        result = self.decide(out["approval_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("להתחבר מחדש", result["error"])
        self.assertEqual(self.posts(), [])

    def test_summarize_uses_the_proposal_line(self):
        self.assertEqual(self.ap.summarize("super_hist_cart_write", {"summary": "הסופר החברתי: x"}),
                         "הסופר החברתי: x")


# --------------------------------------------------------------------------- wiring

class Wiring(unittest.TestCase):
    def test_routes(self):
        router = Router()
        sh.register_routes(router)
        for key in (("GET", "/v1/super-hist/cart"), ("POST", "/v1/super-hist/search"),
                    ("POST", "/v1/proposals/super-hist-cart/add"), ("POST", "/v1/proposals/super-hist-cart/set-qty"),
                    ("POST", "/v1/proposals/super-hist-cart/remove")):
            self.assertIn(key, router._routes)

    def test_mcp_tools(self):
        from command_center.mcp_tools import super_hist as tools
        registered, calls = {}, []
        mcp = types.SimpleNamespace(tool=lambda: (lambda fn: registered.setdefault(fn.__name__, fn)))
        client = types.SimpleNamespace(request=lambda *a, **k: calls.append(a) or {})
        tools.register(mcp, client)
        for name in ("super_hist_cart_view", "super_hist_search", "super_hist_cart_add",
                     "super_hist_cart_set_qty", "super_hist_cart_remove"):
            self.assertIn(name, registered)
        registered["super_hist_cart_add"]("900404", 2)
        registered["super_hist_cart_view"]()
        self.assertEqual(calls[0], ("POST", "/proposals/super-hist-cart/add", {"product_id": "900404", "quantity": 2}))
        self.assertEqual(calls[1], ("GET", "/super-hist/cart"))
        self.assertIn("[[CC_APPROVAL:<approval_id>]]", registered["super_hist_cart_add"].__doc__)

    def test_hermes_include_list(self):
        cfg = (HERE.parent / "hermes" / "config.yaml").read_text()
        for name in ("super_hist_cart_view", "super_hist_search", "super_hist_cart_add",
                     "super_hist_cart_set_qty", "super_hist_cart_remove"):
            self.assertIn(f"- {name}\n", cfg)


if __name__ == "__main__":
    unittest.main()
