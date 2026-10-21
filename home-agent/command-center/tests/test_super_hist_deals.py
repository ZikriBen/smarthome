"""הסופר החברתי deals scan, offline: SYNTHETIC fixtures (tests/fixtures/super-hist-deals) + fake site.

No network. Covers ribbon parsing, deal detection, the assumed promo-category parser, interest ordering and
ranking, kashrut flags, non-food skipping, the request budget, login_required / 403 / site_changed handling,
the real transport (policy + >=10 s throttle + 24 h stop) for the deals' reads, the async job, the saved
report, the CLI and the Hermes wiring.
"""
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from command_center import super_hist as sh  # noqa: E402
from command_center import super_hist_deals as deals  # noqa: E402
from command_center.router import Router  # noqa: E402

FX = Path(__file__).parent / "fixtures" / "super-hist-deals"
TODAY = dt.date(2026, 10, 21)


def by_id(items):
    return {d["product_id"]: d for d in items}


class PromoText(unittest.TestCase):
    def test_multi_buy(self):
        p = deals.parse_promo("2 ב-10", 7.90)
        self.assertEqual((p["n"], p["total"], p["unit"], p["pct"]), (2, 10.0, 5.0, 37))
        self.assertEqual(deals.parse_promo("3 ב 20₪", 9.9)["unit"], 6.67)
        self.assertEqual(deals.parse_promo("2 ב-7.90", 5)["total"], 7.9)

    def test_one_plus_one_and_second_half(self):
        self.assertEqual(deals.parse_promo("1+1", 14.90)["unit"], 7.45)
        self.assertEqual(deals.parse_promo("1+1")["pct"], 50)
        p = deals.parse_promo("השני ב-50%", 10)
        self.assertEqual((p["n"], p["pct"], p["unit"]), (2, 25, 7.5))

    def test_percent_and_price_only(self):
        self.assertEqual(deals.parse_promo("20% הנחה", 10)["unit"], 8.0)
        p = deals.parse_promo("₪36.90", 42.90)
        self.assertEqual((p["unit"], p["pct"]), (36.9, 14))
        self.assertIsNone(deals.parse_promo("₪50", 42.90)["unit"])   # not cheaper than shelf -> just text

    def test_dates(self):
        self.assertEqual(deals.parse_promo("2 ב-7.90 עד 31.10", 5, TODAY)["until"], "2026-10-31")
        self.assertEqual(deals.parse_promo("בתוקף עד 5/11/26", None, TODAY)["until"], "2026-11-05")
        self.assertEqual(deals.parse_promo("עד 15.1", None, TODAY)["until"], "2027-01-15")  # next year
        self.assertIsNone(deals.parse_promo("2 ב-7.90", 5, TODAY)["until"])                  # a price, not a date

    def test_unknown_text_kept(self):
        p = deals.parse_promo("מבצע מועדון", 10)
        self.assertEqual(p["terms"], "מבצע מועדון")
        self.assertIsNone(p["unit"])


class DealOf(unittest.TestCase):
    def prod(self, **kw):
        base = {"product_id": "1", "name": "x", "price": 10.0, "shelf_price": 10.0, "promo": None,
                "out_of_stock": False, "in_cart": False, "weighted": False, "url": None}
        base.update(kw)
        return base

    def test_not_a_deal(self):
        self.assertIsNone(deals.deal_of(self.prod()))

    def test_sale_price(self):
        d = deals.deal_of(self.prod(price=8.0))
        self.assertEqual((d["sale_price"], d["effective_unit"], d["pct_off"]), (8.0, 8.0, 20))
        self.assertEqual(d["url"], "https://super-hist.co.il/index.php?route=product/product&product_id=1")

    def test_best_of_sale_and_ribbon(self):
        d = deals.deal_of(self.prod(price=9.0, promo="2 ב-12"))
        self.assertEqual((d["effective_unit"], d["multi_n"], d["pct_off"]), (6.0, 2, 40))


class Category(unittest.TestCase):
    def test_real_markup(self):
        products, more = deals.parse_category_page((FX / "promo-p1.html").read_text(), 1)
        got = by_id(products)
        self.assertFalse(more)                                           # no pagination on the page
        self.assertEqual(len(products), 9)                               # the JS template is not a card
        self.assertEqual((got["910101"]["price"], got["910101"]["shelf_price"]), (5.9, 7.1))
        self.assertTrue(got["910101"]["on_sale"])
        self.assertEqual(got["910101"]["unit_text"], "1 ליטר (₪5.9 ל 1 ליטר)")
        self.assertTrue(got["910101"]["url"].endswith("/משלמים-פחות-מקבלים-יותר/חלב-3-1-ליטר/"))
        self.assertEqual(got["910103"]["sticker"], "2 ב-18")             # alt text kept
        self.assertIsNone(got["910105"]["sticker"])                      # image-only sticker: no text
        self.assertFalse(got["910104"]["on_sale"])                       # no <del> = no deal
        self.assertIsNone(deals.deal_of(got["910104"]))
        self.assertTrue(got["910107"]["out_of_stock"])                   # from the embedded product JSON
        self.assertTrue(got["910108"]["in_cart"])                        # data-cart-id present (value not kept)
        self.assertNotIn("11111111", json.dumps(products))

    def test_live_sample_cards(self):
        promo, more = deals.parse_category_page((FX / "live-sample-promo-p1.html").read_text(), 1)
        self.assertFalse(more)
        self.assertEqual([p["product_id"] for p in promo], ["1041340", "1055977", "1070134", "940733"])
        rice = promo[0]
        self.assertEqual((rice["name"], rice["price"], rice["shelf_price"]), ("אורז יסמין - DAAWAT", 7.9, 9.9))
        self.assertEqual(rice["unit_text"], '1 ק"ג (₪7.9 ל 1 ק"ג)')
        self.assertIsNone(rice["sticker"])                               # live stickers: alt=""
        col, _ = deals.parse_category_page((FX / "live-sample-cost_of_living-p1.html").read_text(), 1)
        self.assertEqual(by_id(col)["988259"]["shelf_price"], 19.9)
        self.assertEqual(deals.deal_of(by_id(col)["988259"])["pct_off"], 60)

    def test_pagination_only_warns(self):
        page = (FX / "promo-p1.html").read_text().replace(
            "</body>", '<ul class="pagination"><li><a href="?page=2">2</a></li></ul></body>')
        _p, more = deals.parse_category_page(page, 1)
        self.assertTrue(more)

    def test_unknown_markup_is_site_changed(self):
        with self.assertRaises(sh.SiteChanged):
            deals.parse_category_page("<html><body><div class='other'>אין כאן מוצרים</div></body></html>", 1)
        with self.assertRaises(sh.SiteChanged):                           # template-only = no real cards
            deals.parse_category_page('<script type="text/template"><div data-product_id="{{id}}" '
                                      'class="product-item"></div></script>', 1)


class FakeLogin:
    def __init__(self, ok=True, reason=None, on_success=None):
        self.ok, self.reason, self.on_success, self.calls = ok, reason, on_success, 0

    def attempt(self):
        self.calls += 1
        if self.ok and self.on_success:
            self.on_success()
        return self.ok, self.reason


class FakeOps:
    def __init__(self, cart_exc=None, page_exc=None, page_exc_at=None, logged_out=False):
        self.inner = deals.FixtureSite(FX)
        self.cart_exc, self.page_exc, self.page_exc_at = cart_exc, page_exc, page_exc_at
        self.calls = []
        self.logged_out = logged_out

    def cart(self):
        self.calls.append("cart")
        if self.logged_out:
            raise sh.LoginRequired("login")
        if self.cart_exc:
            raise self.cart_exc
        return self.inner.cart()

    def category_page(self, key, page):
        self.calls.append((key, page))
        if self.logged_out:
            raise sh.LoginRequired("login")
        if self.page_exc and (self.page_exc_at is None or self.page_exc_at == (key, page)):
            raise self.page_exc
        return self.inner.category_page(key, page)


def scenario_run(site=None, **kw):
    spec = json.loads((FX / "scenario.json").read_text())
    from grocery.defaults import load_defaults
    d = load_defaults(str((FX / spec["defaults"]).resolve()), user_path=str(FX / "none.yaml"))
    args = dict(list_entries=[{"item": t} for t in spec["list"]], regulars=spec["regulars"],
                staples=spec["staples"], defaults=d, kashrut_level=spec["kashrut"], site=site or FakeOps(),
                categories=["promo"], previous_ids=set(spec["previous_ids"]), today=TODAY,
                login=FakeLogin(ok=False, reason="test: no login expected"))
    args.update(kw)
    return deals.run(**args)


class Standalone(unittest.TestCase):
    """Default: both categories, page 1 only — no cart, no list, no regulars; best value first."""

    def test_no_cart_no_list(self):
        ops = FakeOps()
        r = scenario_run(site=ops)
        self.assertEqual(ops.calls, [("promo", 1)])                       # page 1 only, no cart
        self.assertFalse(r["personal"])
        self.assertEqual(r["interests"], [])
        self.assertEqual(r["notable"], [])
        self.assertEqual(r["requests"], 1)

    def test_ranking_food_first_then_pct(self):
        r = scenario_run()
        ds = r["deals"]
        food = [d for d in ds if not d["nonfood"]]
        self.assertEqual(ds[:len(food)], food)                            # non-food after all food
        pcts = [d["pct_off"] for d in food]
        self.assertEqual(pcts, sorted(pcts, reverse=True))
        self.assertEqual(ds[0]["product_id"], "910105")                   # 7.45 instead of 14.90 = 50%
        self.assertEqual(ds[-1]["product_id"], "910106")                  # shampoo: non-food, last
        self.assertNotIn("910104", by_id(ds))                             # no <del>: not a deal
        self.assertNotIn("910107", by_id(ds))                             # out of stock
        self.assertTrue(all(d["tier"] == deals.TIER_ALL for d in ds))

    def test_top_cap(self):
        saved = deals.TOP_DEALS
        deals.TOP_DEALS = 3
        try:
            self.assertEqual(len(scenario_run()["deals"]), 3)
        finally:
            deals.TOP_DEALS = saved

    def test_pagination_warning_reads_page_1_only(self):
        class PagedOps(FakeOps):
            def category_page(self, key, page):
                products, _ = super().category_page(key, page)
                return products, True
        ops = PagedOps()
        r = scenario_run(site=ops)
        self.assertEqual(ops.calls, [("promo", 1)])
        self.assertIn("נקרא רק עמוד 1", " ".join(r["warnings"]))

    def test_both_categories_dedupe(self):
        ops = FakeOps()
        r = scenario_run(site=ops, categories=["promo", "cost_of_living"])
        self.assertEqual(ops.calls, [("promo", 1), ("cost_of_living", 1)])
        ids = [d["product_id"] for d in r["deals"]]
        self.assertIn("910901", ids)
        self.assertEqual(ids.count("910101"), 1)                          # in both categories: once
        self.assertEqual(r["pages"], {"promo": 1, "cost_of_living": 1})

    def test_both_categories_on_by_default(self):
        saved = {k: os.environ.pop(k, None) for k in deals.CATEGORY_FLAGS.values()}
        try:
            self.assertEqual(deals.enabled_categories(), ["promo", "cost_of_living"])
            os.environ["SUPER_HIST_DEALS_COST_OF_LIVING"] = "0"
            self.assertEqual(deals.enabled_categories(), ["promo"])
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_no_kashrut_by_default(self):
        r = scenario_run()
        self.assertTrue(all(d["kashrut_flag"] is None for d in r["deals"]))
        self.assertNotIn("🔍", r["message"])

    def test_new_since_last_report(self):
        got = by_id(scenario_run()["deals"])
        self.assertFalse(got["910401"]["new"])
        self.assertTrue(got["910101"]["new"])
        self.assertIsNone(by_id(scenario_run(previous_ids=set())["deals"])["910101"]["new"])

    def test_message(self):
        msg = scenario_run()["message"]
        self.assertTrue(msg.startswith("🏷️ מבצעים בסופר החברתי — 21.10"))
        self.assertIn("המבצעים הכי משתלמים", msg)
        self.assertNotIn("על מה שאתה קונה", msg)
        self.assertIn("ממרח שוקולד - השחר — ₪7.45 במקום ₪14.90 (‎-50%)", msg)
        self.assertIn("מדבקה: 2 ב-18", msg)
        self.assertIn("200 גרם (₪4.95 ל 100 גרם)", msg)
        self.assertNotIn("תוקף לא מוצג", msg)
        self.assertIn("נסרקו: משלמים פחות מקבלים יותר ·", msg)
        self.assertNotRegex(msg, r"(?<!\d)\d{9}(?!\d)")

    def test_live_sample_run(self):
        r = deals.run_fixtures(FX, prefix="live-sample-")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(r["deals"]), 8)
        self.assertEqual(r["deals"][0]["product_id"], "1062130")          # canola oil ₪2.50 instead of ₪9.90


class Personal(unittest.TestCase):
    """Optional (flag): read the cart and match against list / regulars / staples."""

    def test_tiers(self):
        ops = FakeOps()
        r = scenario_run(site=ops, personal=True)
        self.assertEqual(ops.calls, ["cart", ("promo", 1)])
        got = by_id(r["deals"])
        self.assertEqual(got["910101"]["tier"], deals.TIER_LIST)
        self.assertTrue(got["910101"]["in_cart"])
        self.assertEqual(got["910103"]["tier"], deals.TIER_LIST)
        self.assertEqual(got["910801"]["tier"], deals.TIER_CART)          # cart line <del>/<ins>
        self.assertEqual(got["910108"]["tier"], deals.TIER_CART)          # data-cart-id on the category card
        self.assertEqual(got["910105"]["tier"], deals.TIER_STAPLE)
        self.assertEqual(got["910401"]["tier"], deals.TIER_STAPLE)
        for pid in ("910104", "910106", "910107"):                       # no del / non-food / out of stock
            self.assertNotIn(pid, got)
        self.assertEqual(r["notable"], [])
        self.assertIn("על מה שאתה קונה", r["message"])

    def test_kashrut_flag_only_with_env(self):
        saved = deals.KASHRUT_ENABLED
        deals.KASHRUT_ENABLED = True
        try:
            got = by_id(scenario_run(personal=True)["deals"])
            self.assertEqual(got["910101"]["kashrut_flag"], "כשרות לא ידועה")
            self.assertIsNone(got["910801"]["kashrut_flag"])               # בד"ץ in the name
        finally:
            deals.KASHRUT_ENABLED = saved
        self.assertTrue(all(d["kashrut_flag"] is None for d in scenario_run(personal=True)["deals"]))


class Failures(unittest.TestCase):
    def test_logged_out_auto_login_then_scan(self):
        ops = FakeOps(logged_out=True)
        login = FakeLogin(ok=True, on_success=lambda: setattr(ops, "logged_out", False))
        r = scenario_run(site=ops, login=login)
        self.assertEqual((r["status"], login.calls), ("ok", 1))
        self.assertEqual(ops.calls, [("promo", 1), ("promo", 1)])          # same page once more
        self.assertTrue(r["auto_login"])
        self.assertIn("התחברתי אוטומטית", r["message"])
        self.assertNotIn("לשלוח קוד", r["message"])

    def test_login_failure_is_one_line(self):
        ops = FakeOps(logged_out=True)
        login = FakeLogin(ok=False, reason="קוד ה-SMS לא הגיע תוך 3 דקות")
        r = scenario_run(site=ops, login=login)
        self.assertEqual((r["status"], login.calls), ("login_failed", 1))
        self.assertEqual(ops.calls, [("promo", 1)])
        self.assertNotIn("\n", r["message"])
        self.assertIn("לא הגיע תוך 3 דקות", r["message"])
        self.assertNotIn("לשלוח קוד", r["message"])

    def test_single_attempt_when_still_logged_out(self):
        ops = FakeOps(logged_out=True)
        login = FakeLogin(ok=True)                       # "succeeds" but the site still says logged out
        r = scenario_run(site=ops, login=login, categories=["promo", "cost_of_living"])
        self.assertEqual((r["status"], login.calls), ("login_failed", 1))
        self.assertEqual(ops.calls, [("promo", 1), ("promo", 1)])

    def test_personal_login_at_cart(self):
        ops = FakeOps(logged_out=True)
        login = FakeLogin(ok=False, reason="x")
        r = scenario_run(site=ops, personal=True, login=login)
        self.assertEqual((r["status"], ops.calls, login.calls), ("login_failed", ["cart"], 1))


class AutoLoginFlow(unittest.TestCase):
    """The deals scan uses THE shared super_hist._auto_login (cart tools / weekly basket): mocked here."""

    def test_delegates_to_shared_helper_once(self):
        calls = []
        login = deals.AutoLogin(login=lambda: calls.append(1) or {"status": "logged_in", "login": "auto"})
        self.assertEqual(login.attempt(), (True, None))
        self.assertEqual(len(calls), 1)
        self.assertIs(deals.AutoLogin().login, sh._auto_login)
        self.assertEqual(deals.LOGIN_MAX_WAIT, 180)

    def test_failure_reason(self):
        login = deals.AutoLogin(login=lambda: sh._login_failed("לא הגיע קוד תוך 3 דק׳"))
        self.assertEqual(login.attempt(), (False, "לא הגיע קוד תוך 3 דק׳"))

    def test_real_helper_timeout_one_sms(self):
        """sh._auto_login with start_login mocked and a fake clock: one SMS, ≤3 min, then login_failed."""
        clock = [1000.0]
        saved = (sh._now, sh._sleep, sh.start_login, sh._inbox_enabled, dict(sh._state))
        starts = []

        def start(p):
            starts.append(p)
            sh._state.update(phase="code_sent", sent_at=clock[0])
            return {"status": "code_sent"}
        sh._now, sh._sleep = (lambda: clock[0]), (lambda x: clock.__setitem__(0, clock[0] + x))
        sh.start_login, sh._inbox_enabled = start, (lambda: False)
        sh._state.update(phase="idle")
        try:
            ok, reason = deals.AutoLogin().attempt()
        finally:
            sh._now, sh._sleep, sh.start_login, sh._inbox_enabled = saved[:4]
            sh._state.clear()
            sh._state.update(saved[4])
        self.assertFalse(ok)
        self.assertIn("לא הגיע קוד", reason)
        self.assertEqual(len(starts), 1)
        self.assertLessEqual(clock[0] - 1000.0, sh.LOGIN_BUDGET + 1)


class SharedStop(unittest.TestCase):
    """ONE 24 h stop: tripped by any path, persisted through the store, seen by the deals scan."""

    def setUp(self):
        self.saved = (sh._block_store[0], sh._blocked_until[0])
        self.db = {}
        sh.use_persistent_block(get=lambda: self.db.get("until"),
                                put=lambda reason: self.db.update(until=sh._now() + sh.BLOCK_SECONDS, reason=reason))
        sh._blocked_until[0] = 0.0

    def tearDown(self):
        sh._block_store[0], sh._blocked_until[0] = self.saved

    def test_trip_persists_and_sync_loads(self):
        sh._trip("HTTP 429 / challenge")
        self.assertIn("429", self.db["reason"])
        sh._blocked_until[0] = 0.0                         # e.g. after a restart
        self.assertTrue(sh.blocked_now())
        self.assertEqual(deals.start({})["status"], "site_blocked")

    def test_persisted_block_stops_scan_before_any_request(self):
        self.db["until"] = sh._now() + 3600
        with self.assertRaises(sh.SiteBlocked):
            sh._cart_send("GET", deals.PROMO_URL)          # no _send: blocked before the socket layer


class Failures2(unittest.TestCase):
    def test_blocked_mid_scan_stops(self):
        ops = FakeOps(page_exc=sh.SiteBlocked("האתר חסם את הבקשה (HTTP 429)"), page_exc_at=("cost_of_living", 1))
        r = scenario_run(site=ops, categories=["promo", "cost_of_living"])
        self.assertEqual((r["status"], r["stopped"]), ("partial", "site_blocked"))
        self.assertEqual(ops.calls, [("promo", 1), ("cost_of_living", 1)])
        self.assertIn("עצרתי באמצע", r["message"])
        self.assertIn("910101", by_id(r["deals"]))

    def test_markup_changed_is_a_warning(self):
        ops = FakeOps(page_exc=sh.SiteChanged("דף המבצעים באתר לא במבנה הצפוי; דילגתי עליו."),
                      page_exc_at=("cost_of_living", 1))
        r = scenario_run(site=ops, categories=["promo", "cost_of_living"])
        self.assertEqual(r["status"], "ok")
        self.assertIn("דילגתי", " ".join(r["warnings"]))


class Transport(unittest.TestCase):
    """SiteOps over the REAL policy + throttle + breaker, with only the socket layer faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (sh.DATA_DIR, sh.COOKIES_FILE, sh._now, sh._sleep, sh._send)
        sh.DATA_DIR, sh.COOKIES_FILE = root, root / "cookies.txt"
        sh.COOKIES_FILE.write_text("# Netscape HTTP Cookie File\n")
        sh._jar = None
        sh._state.update(phase="logged_in", checked_at=0.0)
        sh._blocked_until[0] = 0.0
        sh._last_request[0] = 0.0
        self.clock = [3_000_000.0]
        sh._now = lambda: self.clock[0]
        sh._sleep = lambda s: self.clock.__setitem__(0, self.clock[0] + s)
        self.calls, self.page_status, self.account_status = [], 200, 200
        sh._send = self.fake_send

    def tearDown(self):
        (sh.DATA_DIR, sh.COOKIES_FILE, sh._now, sh._sleep, sh._send) = self.saved
        sh._jar = None
        sh._blocked_until[0] = 0.0
        sh._state.update(checked_at=0.0)
        self.tmp.cleanup()

    def fake_send(self, method, url, form=None, ajax=False, follow=True, referer=None):
        sh.check_url(method, url)
        sh._throttle()
        u = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(u.path)
        self.calls.append((method, sh._route(u.query) or path, u.query, self.clock[0]))
        if sh._route(u.query) == "account/account":
            return self.account_status, {}, "<html>account</html>"
        for key, cat_path in deals.CATEGORY_PATHS.items():
            if path == cat_path:
                if self.page_status != 200:
                    return self.page_status, {}, ""
                f = FX / f"{key}-p1.html"
                return 200, {}, f.read_text() if f.exists() else "<html></html>"
        return 404, {}, ""

    def test_reads_only_and_spacing(self):
        r = scenario_run(site=deals.SiteOps(), categories=["promo", "cost_of_living"])
        self.assertEqual(r["status"], "ok")
        routes = [(m, route, q) for m, route, q, _t in self.calls]
        self.assertEqual(routes, [("GET", "account/account", "route=account/account"), ("GET", deals.PROMO_PATH, ""),
                                  ("GET", deals.COST_OF_LIVING_PATH, "")])
        times = [t for *_x, t in self.calls]
        self.assertTrue(all(b - a >= sh.MIN_INTERVAL for a, b in zip(times, times[1:])), times)

    def test_logged_out_one_login_attempt(self):
        self.account_status = 302
        login = FakeLogin(ok=False, reason="test")
        r = scenario_run(site=deals.SiteOps(), login=login)
        self.assertEqual((r["status"], login.calls), ("login_failed", 1))
        self.assertEqual([c[1] for c in self.calls], ["account/account"])   # no category page, no SMS route

    def test_429_stops_for_24h(self):
        self.page_status = 429
        r = scenario_run(site=deals.SiteOps())
        self.assertEqual(r["stopped"], "site_blocked")
        self.assertGreaterEqual(sh._blocked_until[0], self.clock[0] + sh.BLOCK_SECONDS - 60)
        n = len(self.calls)
        r2 = scenario_run(site=deals.SiteOps())
        self.assertEqual(r2["stopped"], "site_blocked")
        self.assertEqual(len(self.calls), n)                              # nothing sent while stopped

    def test_limit_flag_unverified_off_by_default(self):
        self.assertFalse(deals.LIMIT_100)
        deals.LIMIT_100 = True
        try:
            scenario_run(site=deals.SiteOps())
        finally:
            deals.LIMIT_100 = False
        self.assertEqual([q for _m, _r, q, _t in self.calls], ["route=account/account", "limit=100"])
        with self.assertRaises(sh.PolicyError):
            deals.SiteOps().category_page("promo", 2)

    def test_category_policy(self):
        for url in (deals.PROMO_URL, sh.BASE + deals.PROMO_PATH, deals.PROMO_URL + "?limit=100",
                    deals.CATEGORY_URLS["cost_of_living"], deals.CATEGORY_URLS["cost_of_living"] + "?limit=100"):
            sh.check_url("GET", url)
        for method, url in (("POST", deals.PROMO_URL), ("GET", deals.PROMO_URL + "?route=checkout/checkout"),
                            ("GET", deals.PROMO_URL + "?page=2"), ("GET", deals.PROMO_URL + "?limit=1000"),
                            ("GET", deals.PROMO_URL + "?limit=100&page=2"),
                            ("GET", deals.PROMO_URL + "?sort=p.price&order=ASC"),
                            ("GET", sh.BASE + urllib.parse.quote("/מותג-הבית-הסופר-החברתי/")),
                            ("GET", sh.BASE + "/index.php?route=product/special")):
            with self.assertRaises(sh.PolicyError, msg=url):
                sh.check_url(method, url)


class Job(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (deals.OUT_DIR, deals.scan, sh._block_store[0], os.environ.get("SUPER_HIST_DEALS_PROMO_PAGES"))
        deals.OUT_DIR = Path(self.tmp.name)
        sh._block_store[0] = None
        os.environ["SUPER_HIST_DEALS_PROMO_PAGES"] = "1"
        sh._blocked_until[0] = 0.0

    def tearDown(self):
        deals.OUT_DIR, deals.scan, sh._block_store[0], flag = self.saved
        if flag is None:
            os.environ.pop("SUPER_HIST_DEALS_PROMO_PAGES", None)
        else:
            os.environ["SUPER_HIST_DEALS_PROMO_PAGES"] = flag
        self.tmp.cleanup()

    def test_start_status(self):
        deals.scan = lambda p: scenario_run()
        started = deals.start({})
        self.assertEqual(started["status"], "running")
        self.assertEqual(started["planned"]["requests"], 1 + 2)          # account check + 2 category pages
        out = deals.status({"run_id": started["run_id"], "wait_seconds": 10})
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["text"].startswith("🏷️"))

    def test_disabled_invalid_blocked(self):
        os.environ["SUPER_HIST_DEALS_PROMO_PAGES"] = "0"
        os.environ["SUPER_HIST_DEALS_COST_OF_LIVING"] = "0"
        try:
            self.assertEqual(deals.start({})["status"], "disabled")      # both switched off
        finally:
            os.environ.pop("SUPER_HIST_DEALS_COST_OF_LIVING", None)
        os.environ["SUPER_HIST_DEALS_PROMO_PAGES"] = "1"
        self.assertEqual(deals.start({"max_pages": 2})["status"], "invalid")    # page 1 only
        self.assertEqual(deals.start({"max_pages": "x"})["status"], "invalid")
        sh._blocked_until[0] = time.time() + 100
        try:
            self.assertEqual(deals.start({})["status"], "site_blocked")
        finally:
            sh._blocked_until[0] = 0.0
        self.assertEqual(deals.status({"run_id": "nope"})["status"], "unknown_run")

    def test_standalone_scan_reads_no_personal_sources(self):
        saved = (deals._sources, deals.run)
        seen = {}
        deals._sources = lambda: self.fail("standalone scan must not read list/cart/regulars")
        real_run = saved[1]
        deals.run = lambda *a, **k: seen.update(k) or real_run(site=FakeOps(), categories=["promo"], today=TODAY,
                                                                 login=FakeLogin(ok=False))
        try:
            deals.scan({})
        finally:
            deals._sources, deals.run = saved
        self.assertFalse(seen["personal"])

    def test_save_and_last_and_previous(self):
        self.assertEqual(deals.last()["status"], "none")
        deals._save(scenario_run())
        self.assertTrue((deals.OUT_DIR / "last.md").read_text().startswith("🏷️"))
        self.assertEqual(deals.last()["status"], "ok")
        self.assertIn("910101", deals._previous_ids())

    def test_cached_list_only(self):
        src = Path(deals.__file__).read_text()
        self.assertNotIn("grocery_list.sync", src)
        self.assertNotIn("sync()", src)

    def test_routes(self):
        router = Router()
        deals.register_routes(router)
        for key in (("POST", "/v1/super-hist/deals"), ("POST", "/v1/super-hist/deals/status"),
                    ("GET", "/v1/super-hist/deals/last")):
            self.assertIn(key, router._routes)


class Wiring(unittest.TestCase):
    def test_mcp_tools(self):
        from command_center.mcp_tools import super_hist as tools
        registered, calls = {}, []
        mcp = types.SimpleNamespace(tool=lambda: (lambda fn: registered.setdefault(fn.__name__, fn)))
        client = types.SimpleNamespace(request=lambda *a, **k: calls.append(a) or {})
        tools.register(mcp, client)
        registered["super_hist_deals"]()
        registered["super_hist_deals_status"]("abc", 30)
        self.assertEqual(calls[0], ("POST", "/super-hist/deals", {}))
        self.assertEqual(calls[1], ("POST", "/super-hist/deals/status", {"run_id": "abc", "wait_seconds": 30}))
        self.assertIn("Nothing is added to the cart", registered["super_hist_deals"].__doc__)

    def test_hermes_include_list(self):
        cfg = (HERE.parent / "hermes" / "config.yaml").read_text()
        for name in ("super_hist_deals", "super_hist_deals_status"):
            self.assertIn(f"- {name}\n", cfg)

    def test_api_registers_routes(self):
        self.assertIn("super_hist_deals", (HERE / "command_center" / "api.py").read_text())

    def test_cli_offline(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = deals.main(["--fixtures", str(FX)])
        self.assertEqual(code, 0)
        self.assertIn("המבצעים הכי משתלמים", buf.getvalue())

    def test_combined_soul_snippet(self):
        text = (HERE.parent / "docs" / "SOUL_GROCERY_DEALS_COMBINED_SNIPPET.md").read_text()
        for needle in ("super_hist_deals_status", "grocery_cart_status", "super_hist_cart_view",
                       "Never order or pay; cart changes only via the approval flow", "[[CC_APPROVAL:"):
            self.assertIn(needle, text)
        self.assertNotIn("לשלוח קוד", text)

    def test_soul_snippet(self):
        text = (HERE.parent / "docs" / "SOUL_SUPER_HIST_DEALS_SNIPPET.md").read_text()
        self.assertIn("super_hist_deals_status", text)


class FixturePrivacy(unittest.TestCase):
    def test_no_personal_data(self):
        for path in [p for p in FX.rglob("*") if p.is_file()]:
            text = path.read_text(errors="replace")
            self.assertIsNone(re.search(r"(?<!\d)\d{9}(?!\d)", text), path.name)
            self.assertIsNone(re.search(r"(?<!\d)05\d[- ]?\d{7}(?!\d)", text), path.name)
            self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), path.name)
            self.assertIsNone(re.search(r"PHPSESSID|Set-Cookie|cf_clearance|__cf_bm", text, re.I), path.name)
            self.assertNotIn("data-cart-id=\"4", text, path.name)          # no real cart-line ids
            if path.name.startswith("live-sample-"):
                self.assertIn("LIVE SAMPLE 2026-10-21", text, path.name)
            else:
                self.assertIn("SYNTHETIC", text, path.name)


if __name__ == "__main__":
    unittest.main()
