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
    def test_assumed_markup(self):
        products = deals.parse_category((FX / "promo.html").read_text())
        got = by_id(products)
        self.assertEqual(set(got), {"910901", "910902", "910903"})
        self.assertEqual((got["910901"]["price"], got["910901"]["shelf_price"], got["910901"]["promo"]),
                         (8.9, 9.9, "3 ב-20"))
        self.assertEqual(got["910902"]["name"], 'אורז פרסי 1 ק"ג')
        self.assertTrue(got["910902"]["url"].endswith("product_id=910902"))

    def test_unknown_markup_is_site_changed(self):
        with self.assertRaises(sh.SiteChanged):
            deals.parse_category("<html><body><div class='other'>אין כאן מוצרים</div></body></html>")


class FakeOps:
    """Site ops over the fixtures with switchable failures."""

    def __init__(self, cart_exc=None, search_exc_at=None, search_exc=None, promo_exc=None):
        self.inner = deals.FixtureSite(FX)
        self.cart_exc, self.search_exc_at, self.search_exc, self.promo_exc = cart_exc, search_exc_at, search_exc, promo_exc
        self.calls = []

    def cart(self):
        self.calls.append("cart")
        if self.cart_exc:
            raise self.cart_exc
        return self.inner.cart()

    def search(self, term):
        self.calls.append(("search", term))
        if self.search_exc_at is not None and len([c for c in self.calls if c != "cart"]) > self.search_exc_at:
            raise self.search_exc
        return self.inner.search(term)

    def promo_page(self):
        self.calls.append("promo")
        if self.promo_exc:
            raise self.promo_exc
        return self.inner.promo_page()


def scenario_run(site=None, **kw):
    spec = json.loads((FX / "scenario.json").read_text())
    from grocery.defaults import load_defaults
    d = load_defaults(str((FX / spec["defaults"]).resolve()), user_path=str(FX / "none.yaml"))
    args = dict(list_entries=[{"item": t} for t in spec["list"]], regulars=spec["regulars"],
                staples=spec["staples"], defaults=d, kashrut_level=spec["kashrut"], site=site or FakeOps(),
                previous_ids=set(spec["previous_ids"]), today=TODAY)
    args.update(kw)
    return deals.run(**args)


class Ranking(unittest.TestCase):
    def test_interest_order_and_nonfood_skipped(self):
        r = scenario_run()
        terms = [i["term"] for i in r["interests"]]
        self.assertEqual(terms, ["חלב", "ביצים", "קפה טורקי", "פיתות", "ממרח שוקולד", "סוכר לבן", "חומץ"])
        self.assertNotIn("שמפו", " ".join(terms))

    def test_tiers(self):
        r = scenario_run()
        mine = r["deals"]
        tiers = [d["tier"] for d in mine]
        self.assertEqual(tiers, sorted(tiers))
        got = by_id(mine)
        self.assertEqual(got["910101"]["tier"], deals.TIER_LIST)        # on the list (and in the cart)
        self.assertTrue(got["910101"]["in_cart"])
        self.assertEqual(got["910801"]["tier"], deals.TIER_CART)        # cart line shown <del>/<ins>
        self.assertEqual(got["910401"]["tier"], deals.TIER_REGULAR)
        self.assertEqual(got["910501"]["tier"], deals.TIER_STAPLE)
        self.assertNotIn("910601", got)                                 # sugar is not on a deal
        self.assertNotIn("910302", got)                                 # out of stock

    def test_item_rules_keep_lookalikes_out_of_mine(self):
        r = scenario_run()
        mine, notable = by_id(r["deals"]), by_id(r["notable"])
        for pid in ("910103", "910105", "910203", "910702"):            # שוקו / סויה / אטריות ביצים / בלסמי
            self.assertNotIn(pid, mine)
        self.assertIn("910203", notable)                                # 43% off food -> notable
        self.assertIn("910104", notable)                                # cheese 2 ב-9 = 31%

    def test_nonfood_never_listed(self):
        r = scenario_run(promo_pages=True)
        names = " ".join(d["name"] for d in r["deals"] + r["notable"])
        self.assertNotIn("שמפו", names)
        self.assertNotIn("נוזל כלים", names)

    def test_notable_threshold_and_cap(self):
        r = scenario_run(promo_pages=True)
        self.assertTrue(all(d["pct_off"] >= deals.NOTABLE_MIN_PCT for d in r["notable"]))
        self.assertLessEqual(len(r["notable"]), deals.NOTABLE_MAX)

    def test_kashrut_flags(self):
        got = by_id(scenario_run()["deals"] + scenario_run()["notable"])
        self.assertIsNone(got["910101"]["kashrut_flag"])                # בד"ץ in the name
        self.assertIsNone(got["910104"]["kashrut_flag"])                # מהדרין in the name
        self.assertEqual(got["910301"]["kashrut_flag"], "כשרות לא ידועה")
        no_rule = by_id(scenario_run(kashrut_level=None)["deals"])
        self.assertIsNone(no_rule["910301"]["kashrut_flag"])

    def test_new_since_last_report(self):
        got = by_id(scenario_run()["deals"])
        self.assertFalse(got["910401"]["new"])
        self.assertTrue(got["910101"]["new"])
        self.assertIsNone(by_id(scenario_run(previous_ids=set())["deals"])["910101"]["new"])

    def test_message(self):
        r = scenario_run()
        msg = r["message"]
        self.assertTrue(msg.startswith("🏷️ מבצעים בסופר החברתי — 21.10"))
        self.assertIn("על מה שאתה קונה", msg)
        self.assertIn("עוד מבצעים בולטים", msg)
        self.assertIn("₪6.20 במקום ₪7.10", msg)
        self.assertIn("1+1 (₪7.45 ליח׳ במקום ₪14.90)", msg)
        self.assertIn("עד 31.10", msg)
        self.assertIn("https://super-hist.co.il/index.php?route=product/product&product_id=910801", msg)
        self.assertIn("🔍", msg)
        self.assertNotRegex(msg, r"(?<!\d)\d{9}(?!\d)")


class Budget(unittest.TestCase):
    def test_cap_and_not_searched(self):
        ops = FakeOps()
        r = scenario_run(site=ops, max_searches=3)
        self.assertEqual(r["searches"], 3)
        self.assertEqual(len([c for c in ops.calls if c != "cart"]), 3)
        self.assertEqual(r["not_searched"], ["פיתות", "ממרח שוקולד", "סוכר", "חומץ"])
        self.assertIn("לא נבדקו: פיתות", r["message"])
        self.assertEqual(r["requests"], 2 + 3)

    def test_hard_cap(self):
        self.assertEqual(deals.Budget(500).max_searches, deals.HARD_MAX_SEARCHES)

    def test_promo_page_off_by_default(self):
        ops = FakeOps()
        scenario_run(site=ops)
        self.assertNotIn("promo", ops.calls)

    def test_plan(self):
        plan = deals.plan_requests(20, 12)
        self.assertEqual((plan["searches"], plan["requests"]), (12, 16))   # +1 account re-check after 120 s
        self.assertGreaterEqual(plan["seconds"], 15 * sh.MIN_INTERVAL)


class Failures(unittest.TestCase):
    def test_login_required_never_searches(self):
        ops = FakeOps(cart_exc=sh.LoginRequired("login"))
        r = scenario_run(site=ops)
        self.assertEqual(r["status"], "login_required")
        self.assertEqual(ops.calls, ["cart"])
        self.assertIn("SMS", r["message"])

    def test_blocked_mid_scan_stops(self):
        ops = FakeOps(search_exc_at=2, search_exc=sh.SiteBlocked("האתר חסם את הבקשה (HTTP 429)"))
        r = scenario_run(site=ops)
        self.assertEqual((r["status"], r["stopped"]), ("partial", "site_blocked"))
        self.assertEqual(len([c for c in ops.calls if c != "cart"]), 3)   # the failing one, then nothing
        self.assertIn("עצרתי באמצע", r["message"])
        self.assertIn("910101", by_id(r["deals"]))                       # what was found is kept

    def test_promo_markup_changed_is_a_warning(self):
        ops = FakeOps(promo_exc=sh.SiteChanged("דף המבצעים באתר לא במבנה הצפוי; דילגתי עליו."))
        r = scenario_run(site=ops, promo_pages=True)
        self.assertEqual(r["status"], "ok")
        self.assertIn("דילגתי", " ".join(r["warnings"]))

    def test_cart_blocked(self):
        r = scenario_run(site=FakeOps(cart_exc=sh.SiteBlocked("x")))
        self.assertEqual(r["status"], "site_blocked")


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
        self.slept = []
        sh._now = lambda: self.clock[0]
        sh._sleep = lambda s: (self.slept.append(s), self.clock.__setitem__(0, self.clock[0] + s))
        self.calls, self.status_for_search = [], 200
        sh._send = self.fake_send

    def tearDown(self):
        (sh.DATA_DIR, sh.COOKIES_FILE, sh._now, sh._sleep, sh._send) = self.saved
        sh._jar = None
        sh._blocked_until[0] = 0.0
        sh._catalog.clear()
        self.tmp.cleanup()

    def fake_send(self, method, url, form=None, ajax=False, follow=True, referer=None):
        sh.check_url(method, url)
        sh._throttle()
        u = urllib.parse.urlsplit(url)
        self.calls.append((method, sh._route(u.query) or urllib.parse.unquote(u.path), self.clock[0]))
        if u.path == sh.SEARCH_PATH:
            term = (form or {}).get("search", "")
            f = FX / "search" / f"{term}.json"
            return self.status_for_search, {}, f.read_text() if f.exists() and self.status_for_search == 200 else "{}"
        if sh._route(u.query) == "checkout/cart":
            return 200, {}, (FX / "cart.html").read_text()
        if sh._route(u.query) == "account/account":
            return 200, {}, "<html>account</html>"
        if urllib.parse.unquote(u.path) == deals.PROMO_PATH:
            return 200, {}, (FX / "promo.html").read_text()
        return 404, {}, ""

    def test_reads_only_and_spacing(self):
        r = scenario_run(site=deals.SiteOps(), promo_pages=True)
        self.assertEqual(r["status"], "ok")
        methods = {(m, route) for m, route, _t in self.calls}
        self.assertTrue(methods <= {("GET", "account/account"), ("GET", "checkout/cart"),
                                    ("POST", sh.SEARCH_PATH), ("GET", deals.PROMO_PATH)}, methods)
        times = [t for _m, _r, t in self.calls]
        self.assertTrue(all(b - a >= sh.MIN_INTERVAL for a, b in zip(times, times[1:])), times)
        self.assertEqual(len(self.calls), 1 + 1 + 7 + 1)

    def test_429_stops_for_24h(self):
        self.status_for_search = 429
        r = scenario_run(site=deals.SiteOps())
        self.assertEqual(r["stopped"], "site_blocked")
        self.assertGreaterEqual(sh._blocked_until[0], self.clock[0] + sh.BLOCK_SECONDS - 60)
        n = len(self.calls)
        r2 = scenario_run(site=deals.SiteOps())
        self.assertEqual(r2["status"], "site_blocked")
        self.assertEqual(len(self.calls), n)                              # nothing sent while stopped

    def test_promo_path_policy(self):
        sh.check_url("GET", deals.PROMO_URL)
        sh.check_url("GET", deals.PROMO_URL + "?page=2")
        sh.check_url("GET", sh.BASE + deals.PROMO_PATH)                   # unquoted form too
        for method, url in (("POST", deals.PROMO_URL), ("GET", deals.PROMO_URL + "?route=checkout/checkout"),
                            ("GET", deals.PROMO_URL + "?page=2&x=1"),
                            ("GET", sh.BASE + urllib.parse.quote("/יוקר-המחייה/")),
                            ("GET", sh.BASE + "/index.php?route=product/special")):
            with self.assertRaises(sh.PolicyError, msg=url):
                sh.check_url(method, url)


class Job(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (deals.OUT_DIR, deals.scan, deals._persisted_block)
        deals.OUT_DIR = Path(self.tmp.name)
        deals._persisted_block = lambda: None
        sh._blocked_until[0] = 0.0

    def tearDown(self):
        deals.OUT_DIR, deals.scan, deals._persisted_block = self.saved
        self.tmp.cleanup()

    def test_start_status(self):
        deals.scan = lambda p: scenario_run()
        started = deals.start({"max_searches": 5})
        self.assertEqual(started["status"], "running")
        self.assertEqual(started["planned"]["requests"], 8)               # 3 + 5 searches, no re-check
        out = deals.status({"run_id": started["run_id"], "wait_seconds": 10})
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["text"].startswith("🏷️"))
        self.assertNotIn("interests", out)

    def test_invalid_and_blocked(self):
        self.assertEqual(deals.start({"max_searches": 99})["status"], "invalid")
        self.assertEqual(deals.start({"max_searches": "x"})["status"], "invalid")
        sh._blocked_until[0] = time.time() + 100
        sh_now = sh._now
        try:
            self.assertEqual(deals.start({})["status"], "site_blocked")
        finally:
            sh._blocked_until[0] = 0.0
            sh._now = sh_now
        self.assertEqual(deals.status({"run_id": "nope"})["status"], "unknown_run")

    def test_save_and_last_and_previous(self):
        self.assertEqual(deals.last()["status"], "none")
        r = scenario_run()
        deals._save(r)
        self.assertTrue((deals.OUT_DIR / "last.md").read_text().startswith("🏷️"))
        last = deals.last()
        self.assertEqual(last["status"], "ok")
        self.assertIn("910101", deals._previous_ids())

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
        self.assertEqual(calls[0], ("POST", "/super-hist/deals", {"max_searches": 12}))
        self.assertEqual(calls[1], ("POST", "/super-hist/deals/status", {"run_id": "abc", "wait_seconds": 30}))
        self.assertIn("Nothing is added to the cart", registered["super_hist_deals"].__doc__)

    def test_hermes_include_list(self):
        cfg = (HERE.parent / "hermes" / "config.yaml").read_text()
        for name in ("super_hist_deals", "super_hist_deals_status"):
            self.assertIn(f"- {name}\n", cfg)

    def test_api_registers_routes(self):
        src = (HERE / "command_center" / "api.py").read_text()
        self.assertIn("super_hist_deals", src)

    def test_cli_offline(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = deals.main(["--fixtures", str(FX)])
        self.assertEqual(code, 0)
        self.assertIn("על מה שאתה קונה", buf.getvalue())

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
            self.assertIn("SYNTHETIC", text, path.name)


if __name__ == "__main__":
    unittest.main()
