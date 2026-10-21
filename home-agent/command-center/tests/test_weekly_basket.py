"""Weekly basket (Super-Hist first + ONE other store), offline: synthetic Super-Hist fixtures + the real
Rami/Shufersal fixtures (tests/fixtures/grocery-live), fake transports, the FakeSite for the batched cart.
No network: Super-Hist "live" here is always a fake transport."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from grocery import api, report  # noqa: E402
from grocery import weekly as W  # noqa: E402
from grocery.defaults import load_defaults  # noqa: E402
from grocery.delivery import SiteDelivery  # noqa: E402
from grocery.http import CloudflareBlocked, HttpError, SessionExpired  # noqa: E402
from grocery.sites import get_adapter_class  # noqa: E402
from grocery.sites import super_hist as SHA  # noqa: E402

FX = HERE / "tests" / "fixtures" / "grocery-live"
DEFAULTS = str(HERE / "grocery-defaults.yaml")
ALL13 = ("ביצים, פלפל אדום, 5 מלפפון, חלב, ממרח שוקולד, ממרח חלבה, אבקת מרק עוף, פיתות, חומץ, רדיד אלומיניום, "
         "שיבולת שועל, קולה זירו, סוכר לבן")


def sh_fixture(term):
    idx = json.loads((FX / "super_hist" / "index.json").read_text())
    fn = idx.get(term)
    return json.loads((FX / "super_hist" / fn).read_text())["data"] if fn else []


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved_state = os.environ.get("GROCERY_STATE_DIR")
        os.environ["GROCERY_STATE_DIR"] = str(self.root / "state")
        api.clear()
        SHA.set_transport(None)

    def tearDown(self):
        SHA.set_transport(None)
        if self.saved_state is None:
            os.environ.pop("GROCERY_STATE_DIR", None)
        else:
            os.environ["GROCERY_STATE_DIR"] = self.saved_state
        self.tmp.cleanup()

    def items(self, text=ALL13, **kw):
        d = load_defaults(DEFAULTS)
        api.add_item(text, defaults_path=DEFAULTS)
        items, d = api.current_items(DEFAULTS)
        for k, v in kw.items():
            setattr(d, k, v)
        return items, d

    def run_weekly(self, fixtures=str(FX), override=None, **kw):
        if override is None:
            override = self.items()
        log = []
        r = api.weekly(fixtures=fixtures, out_dir=str(self.root / "out"), defaults_path=DEFAULTS, log=log,
                       items_override=override, **kw)
        r["_log"] = log
        return r

    def partial_root(self):
        """Rami/Shufersal fixtures only: Super-Hist goes through the (fake) transport."""
        root = self.root / "fx"
        for s in ("rami_levy", "shufersal"):
            shutil.copytree(FX / s, root / s)
        return str(root)


class Adapter(unittest.TestCase):
    def setUp(self):
        self.ad = get_adapter_class("super_hist")()

    def test_sale_price_weight_stock_kashrut(self):
        rows = {r["product_id"]: r for t in ("ממרח חלבה", "פלפל אדום", "ממרח שוקולד", "קולה זירו") for r in sh_fixture(t)}
        halva = self.ad.normalize(rows["910041"])
        self.assertEqual((halva.price, halva.shelf_price, halva.promo_used), (9.5, 11.9, "מחיר מבצע"))
        self.assertTrue(halva.available)
        self.assertIsNone(halva.kashrut)                                  # no data -> 'כשרות לא ידועה'
        pepper = self.ad.normalize(rows["910011"])
        self.assertTrue(pepper.by_weight)
        self.assertEqual(pepper.sell_method, "weight")
        self.assertFalse(self.ad.normalize(rows["910031"]).available)     # out_of_stock
        self.assertIn("2 ב-11", self.ad.normalize(rows["910101"]).promo_note)
        bad = self.ad.normalize(dict(rows["910041"], name="נקניק לא כשר 200 גרם"))
        self.assertEqual(bad.kashrut, ["לא כשר (לפי שם המוצר)"])
        self.assertIsNone(self.ad.normalize({"product_id": "x"}))

    def test_cart_never_from_package(self):
        with self.assertRaises(NotImplementedError):
            self.ad.cart_merge_post(None, None, {"1": 1})
        with self.assertRaises(NotImplementedError):
            self.ad.load_session()
        self.assertNotIn("checkout/checkout", self.ad.checkout_url)

    def test_searcher_needs_transport_and_has_budget(self):
        with self.assertRaises(SessionExpired):
            SHA.SuperHistSearcher().search("חלב")
        s = SHA.SuperHistSearcher(transport=lambda t: [], budget=2)
        s.search("a"), s.search("b")
        with self.assertRaises(HttpError):
            s.search("c")
        self.assertEqual(s.request_count, 2)


class Assignment(Base):
    def test_super_hist_first_remainder_to_one_store(self):
        r = self.run_weekly()
        self.assertEqual(r["status"], "done")
        res = r["result"]
        sh_items = [x["item"] for x in res["super_hist"]["lines"]]
        self.assertEqual(len(sh_items), 10)
        self.assertIn("חלב", sh_items)              # pricier at Rami, but the default stays Super-Hist
        rem = {x["item"]: x["why_not_sh"] for x in res["remainder"]}
        self.assertEqual(rem, {"מלפפון": "לא נמצא", "ממרח שוקולד": "אזל במלאי", "שיבולת שועל": "לא נמצא"})
        o = res["other"]
        self.assertEqual(o["site"], "rami_levy")    # cheaper for the remainder (incl. delivery)
        self.assertEqual(sorted(x["item"] for x in o["lines"]), sorted(rem))
        self.assertGreater(o["compare"]["shufersal"]["final"], o["compare"]["rami_levy"]["final"])
        # exactly ONE other store link, and it holds only the remainder
        self.assertEqual(list(r["urls"]), ["rami_levy"])
        self.assertEqual(r["urls"]["rami_levy"][0].count(";") + 1, 3)
        self.assertIsNone(r["links"]["super_hist"])       # price-only: no cart was built
        msg = r["message"]
        self.assertIn("1️⃣ הסופר החברתי — 10 פריטים", msg)
        self.assertIn("2️⃣ רמי לוי — 3 פריטים", msg)
        self.assertNotIn("affiliate", msg)
        self.assertIn("מצב מחירים בלבד", msg)
        self.assertAlmostEqual(res["super_hist"]["total"], sum(x["cost"] for x in res["super_hist"]["lines"]))
        self.assertTrue(Path(r["report_path"]).is_file())

    def test_evidence_pricier_lines(self):
        ev = self.run_weekly()["result"]["evidence"]
        rami = {x["item"]: x for x in ev["rami_levy"]["pricier"]}
        self.assertEqual(rami["חלב"]["diff"], 0.80)
        self.assertAlmostEqual(rami["סוכר"]["diff"], 0.20)
        self.assertEqual(ev["rami_levy"]["lines"], 10)
        self.assertEqual(ev["rami_levy"]["sh_pricier"], len(rami))
        self.assertEqual(ev["rami_levy"]["sh_cheaper"] + ev["rami_levy"]["sh_pricier"] + ev["rami_levy"]["same"], 10)
        self.assertIn("ביצים", ev["rami_levy"]["pack_differs"])      # 30 vs 12 eggs: per-unit compare
        self.assertNotIn("ביצים", rami)                               # cheaper per egg at Super-Hist
        self.assertIn("שופרסל", report.weekly_full(self.run_weekly()["result"], []))

    def test_message_shows_evidence_and_pricier(self):
        msg = self.run_weekly()["message"]
        self.assertIn("📊 אותם פריטים של הסופר", msg)
        self.assertIn("חלב (+₪0.80 מול רמי", msg)

    def test_shufersal_chosen_when_cheaper(self):
        items, d = self.items()
        d.delivery = dict(d.delivery, rami_levy=SiteDelivery(fee=100.0))
        r = self.run_weekly(override=(items, d))
        self.assertEqual(r["other_site"], "shufersal")
        self.assertEqual(list(r["urls"]), ["shufersal"])
        self.assertIn("2️⃣ שופרסל", r["message"])

    def kosher_run(self):
        root = Path(self.partial_root())
        items, d = self.items()
        soup = next(i for i in items if i.name == "מרק עוף")
        soup.kashrut = "mehadrin"

        def transport(term):
            rows = sh_fixture(term)
            if term == "אבקת מרק עוף":
                rows = [dict(rows[0], name="אבקת מרק עוף 400 גרם לא כשר")]
            return rows
        SHA.set_transport(transport)
        return self.run_weekly(fixtures=str(root), override=(items, d))

    def test_kashrut_off_by_default_stays_at_super_hist_unmarked(self):
        from grocery import kashrut as K
        os.environ.pop(K.ENV_FLAG, None)
        r = self.kosher_run()
        res = r["result"]
        self.assertIn("מרק עוף", [x["item"] for x in res["super_hist"]["lines"]])
        self.assertFalse(any(x["kashrut_unknown"] for x in res["super_hist"]["lines"]))
        self.assertNotIn("כשרות", r["message"])
        self.assertNotIn("כשר", r["message"].replace("לא כשר", ""))     # only the (synthetic) product name

    def test_kashrut_flag_on_not_kosher_goes_to_remainder(self):
        from grocery import kashrut as K
        os.environ[K.ENV_FLAG] = "1"
        try:
            res = self.kosher_run()["result"]
        finally:
            os.environ.pop(K.ENV_FLAG, None)
        rem = {x["item"]: x["why_not_sh"] for x in res["remainder"]}
        self.assertEqual(rem.get("מרק עוף"), "אין התאמה כשרה")

    def test_to_check_shown_never_counted(self):
        items, d = self.items()
        r = self.run_weekly(override=(items, d), skipped={"rami_levy": "x"})
        res = r["result"]
        # Shufersal is the only other store; anything it lacks is 'לבדיקה' and not in any total
        o = res["other"]
        self.assertEqual(o["site"], "shufersal")
        self.assertAlmostEqual(o["items_total"], sum(x["cost"] for x in o["lines"]))
        for row in res["check"]:
            self.assertNotIn(row["item"], [x["item"] for x in o["lines"]])

    def test_estimate_15_lines(self):
        e = W.estimate(15)
        self.assertEqual(e["searches_per_site"], 16)
        self.assertEqual(e["wall_seconds"], e["super_hist"]["seconds"])     # the slowest site in parallel
        self.assertTrue(150 <= e["wall_seconds"] <= 240, e)
        self.assertLess(e["rami_levy"]["seconds"], 60)
        self.assertGreater(e["shufersal"]["seconds"], 150)
        self.assertEqual(W.estimate(15, build_cart=True)["approval_write"]["requests"], 18)


class SuperHistFailures(Base):
    def test_check_only_gate_login_required_stops_everything(self):
        gate_calls = []
        r = self.run_weekly(super_hist_gate=lambda: gate_calls.append(1) or
                            {"status": "login_required", "text": "🔐 צריך להתחבר"})
        self.assertEqual(r["status"], "login_required")
        self.assertEqual(gate_calls, [1])
        self.assertFalse([m for m in r["_log"] if "start (" in m])   # no site was searched

    def test_auto_login_failed_whole_list_to_other_store(self):
        gate_calls = []
        r = self.run_weekly(super_hist_gate=lambda: gate_calls.append(1) or sh._login_failed("לא הגיע קוד תוך 3 דק׳"))
        self.assertEqual(gate_calls, [1])                         # one gate = one login attempt per run
        self.assertEqual(r["status"], "done")
        self.assertFalse([m for m in r["_log"] if "[super_hist] start" in m or "super_hist start" in m])
        self.assertEqual(len(r["result"]["remainder"]), 13)
        self.assertEqual(r["super_hist_login"], "failed")
        lines = [ln for ln in r["message"].splitlines() if "ההתחברות האוטומטית" in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn("כל הרשימה", lines[0])
        self.assertIn(r["result"]["other"]["name"], lines[0])

    def test_auto_login_success_line(self):
        root = self.partial_root()
        SHA.set_transport(sh_fixture)
        r = self.run_weekly(fixtures=root, super_hist_gate=lambda: {"status": "logged_in", "login": "auto",
                                                                    "requests": 4})
        self.assertEqual(r["super_hist_login"], "auto")
        self.assertIn("התחברתי מחדש אוטומטית", r["message"])
        self.assertTrue(r["result"]["super_hist"]["lines"])
        self.assertGreaterEqual(r["runtime"]["requests"]["super_hist"], 4)

    def test_gate_blocked_skips_super_hist_only(self):
        r = self.run_weekly(super_hist_gate=lambda: {"status": "site_blocked", "text": "HTTP 403"})
        self.assertEqual(r["status"], "done")
        self.assertIn("super_hist", r["site_errors"])
        self.assertEqual(len(r["result"]["remainder"]), 13)
        self.assertIn("1️⃣ הסופר החברתי: לא זמין", r["message"])

    def test_session_lost_mid_run(self):
        calls = []

        def transport(term):
            calls.append(term)
            if len(calls) >= 3:
                raise SessionExpired("login_required: פג")
            return sh_fixture(term)
        SHA.set_transport(transport)
        r = self.run_weekly(fixtures=self.partial_root())
        self.assertEqual(len(calls), 3)                           # stopped at once, no more requests
        self.assertIn("החיבור פג באמצע", r["site_errors"]["super_hist"])
        self.assertNotIn("super_hist", r["blocked"])              # login is not a 24 h block
        self.assertEqual(len(r["result"]["remainder"]), 13)
        self.assertEqual(sum("פג באמצע הריצה" in ln for ln in r["message"].splitlines()), 1)

    def test_blocked_mid_run_goes_to_breaker(self):
        SHA.set_transport(lambda term: (_ for _ in ()).throw(CloudflareBlocked("HTTP 429")))
        r = self.run_weekly(fixtures=self.partial_root())
        self.assertIn("super_hist", r["blocked"])

    def test_one_failed_search_moves_only_that_item(self):
        def transport(term):
            if term == "חלב":
                raise HttpError("האתר לא זמין כרגע")
            return sh_fixture(term)
        SHA.set_transport(transport)
        res = self.run_weekly(fixtures=self.partial_root())["result"]
        rem = {x["item"]: x["why_not_sh"] for x in res["remainder"]}
        self.assertEqual(rem.get("חלב"), "שגיאת חיפוש")
        self.assertEqual(len(res["super_hist"]["lines"]), 9)

    def test_request_budget(self):
        SHA.set_transport(sh_fixture)
        saved = SHA.DEFAULT_BUDGET
        SHA.DEFAULT_BUDGET = 4
        try:
            res = self.run_weekly(fixtures=self.partial_root())["result"]
        finally:
            SHA.DEFAULT_BUDGET = saved
        self.assertLessEqual(len(res["super_hist"]["lines"]), 4)


class CartMode(Base):
    def test_builder_gets_super_hist_lines_and_message_waits_for_approval(self):
        got = []

        def builder(lines):
            got.extend(lines)
            return {"status": "pending_approval", "approval_id": "abc", "headline": "הסופר החברתי: 9 שינויים"}
        r = self.run_weekly(mode="cart", cart_builder=builder)
        self.assertEqual(len(got), 10)
        self.assertTrue(any(x["weighted"] for x in got))         # the builder refuses by-weight lines itself
        self.assertEqual(r["links"]["super_hist"], report.SH_CART_URL)
        self.assertIn("ממתין לאישור", r["message"])
        self.assertNotIn("[[CC_APPROVAL", r["message"])          # Hermes appends the marker

    def test_builder_login_required_keeps_price_part(self):
        r = self.run_weekly(mode="cart", cart_builder=lambda lines: {"status": "login_required", "text": "פג"})
        self.assertIn("לא בניתי עגלה", r["message"])
        self.assertIn("מצב מחירים בלבד", r["message"])


class History(Base):
    def test_history_file_and_summary(self):
        path = self.root / "data" / "weekly-history.jsonl"
        self.assertFalse(path.exists())
        self.run_weekly(history_path=str(path))
        api.clear()
        r2 = self.run_weekly(history_path=str(path))
        rows = W.history_read(str(path))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["super_hist"]["lines"], 10)
        self.assertEqual(rows[0]["other"]["site"], "rami_levy")
        self.assertIn("sh_pricier", rows[0]["evidence"]["rami_levy"])
        self.assertIn("📈 היסטוריה (2 ריצות)", r2["message"])


# --------------------------------------------------------------------------- the batched Super-Hist cart
from test_super_hist_cart import FakeSite, sh  # noqa: E402


class Batch(FakeSite):
    def search(self, *pids):
        self.assertEqual(sh.search({"term": "חטיף"})["status"], "ok")

    def line(self, pid, qty=1, name="x", price=5.0, weighted=False):
        return {"product_id": pid, "quantity": qty, "name": name, "price": price, "weighted": weighted}

    def wait_batch(self, results):
        deadline = time.time() + 10
        while time.time() < deadline:
            if results and results[-1][1].get("status") not in ("running", None):
                return results[-1][1]
            time.sleep(0.02)
        self.fail("batch did not finish")

    def test_one_approval_for_all_lines(self):
        self.search()
        r = sh.propose_batch([self.line("900404", 2, "חטיף"), self.line("900101", 3, "חלב"),
                              self.line("900101", 1, "חלב"), self.line("555", 1, "עגבניות", weighted=True)], "run1")
        self.assertEqual(r["status"], "pending_approval", r)
        self.assertEqual([c for c in self.posts() if c[1].startswith("checkout/")], [])   # nothing written
        ops = {c["name"]: (c["op"], c["qty_before"], c["qty_after"]) for c in r["changes"]}
        self.assertEqual(ops["חטיף"], ("add", 0, 2))
        self.assertEqual(ops["חלב"], ("set_qty", 2, 3))        # raise, never a blind add on an existing line
        reasons = " ".join(x["reason"] for x in r["skipped"])
        self.assertIn("במשקל", reasons)
        self.assertIn("כבר בעגלה", reasons)
        self.assertIn("בעגלה עכשיו 1 שורות", r["text"])         # the current cart is shown first
        rows = self.db.execute("SELECT action FROM approvals").fetchall()
        self.assertEqual(rows, [("super_hist_cart_batch",)])

    def test_approved_batch_writes_and_verifies(self):
        self.search()
        r = sh.propose_batch([self.line("900404", 2, "חטיף"), self.line("900101", 3, "חלב")], "run1")
        results = []
        sh._batch_listeners[:] = [lambda run_id, res: results.append((run_id, res))]
        out = self.decide(r["approval_id"])
        self.assertIn(out["status"], ("running", "executed"))     # background: not "done" yet (fast fake site)
        self.assertEqual(out["result"]["status"], "started")
        res = self.wait_batch(results)
        self.assertEqual(res["status"], "done", res)
        view = self.ap.get(r["approval_id"])
        self.assertEqual(view["status"], "executed")
        self.assertEqual(view["result_text"], res["text"])          # what Hermes pushes to Telegram
        self.assertEqual(len(res["written"]), 2)
        self.assertEqual(results[-1][0], "run1")
        self.assertEqual({ln["pid"]: ln["qty"] for ln in self.lines}, {"900101": 3, "900404": 2})
        self.assertFalse(any("checkout/checkout" in c[1] or "order" in c[1] for c in self.calls))
        self.assertIn("עודכנה", res["text"])

    def test_partial_failure_reported_never_retried(self):
        self.search()
        r = sh.propose_batch([self.line("900404", 2, "חטיף"), self.line("900101", 3, "חלב")], "run1")
        results = []
        sh._batch_listeners[:] = [lambda run_id, res: results.append((run_id, res))]
        self.override["checkout/cart/edit"] = (200, {}, json.dumps({"error": "כמות לא זמינה"}))
        self.decide(r["approval_id"])
        res = self.wait_batch(results)
        self.assertEqual(res["status"], "partial", res)
        self.assertEqual([x["name"] for x in res["written"]], ["חטיף"])
        self.assertEqual([x["name"] for x in res["failed"]], ["חלב"])
        self.assertEqual(len([c for c in self.posts() if c[1] == "checkout/cart/edit"]), 1)   # no retry

    def test_prompt_running_until_worker_finishes(self):
        self.search()
        r = sh.propose_batch([self.line("900404", 2, "חטיף")], "run1")
        gate = threading.Event()
        real = sh._run_batch
        sh._run_batch = lambda ident, payload: (gate.wait(5), real(ident, payload))
        results = []
        sh._batch_listeners[:] = [lambda run_id, res: results.append((run_id, res))]
        try:
            out = self.decide(r["approval_id"])
            self.assertEqual(out["status"], "running")
            self.assertEqual(self.ap.get(r["approval_id"])["status"], "running")
            again = self.decide(r["approval_id"])                   # second tap: the recorded state, no rerun
            self.assertTrue(again["already_decided"])
            self.assertEqual(again["status"], "running")
            gate.set()
            res = self.wait_batch(results)
        finally:
            sh._run_batch = real
        for _ in range(100):
            if self.ap.get(r["approval_id"])["status"] != "running":
                break
            time.sleep(0.02)
        view = self.ap.get(r["approval_id"])
        self.assertEqual(view["status"], "executed")
        self.assertIn("חטיף", view["result_text"])
        self.assertEqual(res["status"], "done")

    def test_partial_result_marks_failed_with_details(self):
        self.search()
        r = sh.propose_batch([self.line("900404", 2, "חטיף"), self.line("900101", 3, "חלב")], "run1")
        results = []
        sh._batch_listeners[:] = [lambda run_id, res: results.append((run_id, res))]
        self.override["checkout/cart/edit"] = (200, {}, json.dumps({"error": "כמות לא זמינה"}))
        self.decide(r["approval_id"])
        self.wait_batch(results)
        for _ in range(100):
            if self.ap.get(r["approval_id"])["status"] != "running":
                break
            time.sleep(0.02)
        view = self.ap.get(r["approval_id"])
        self.assertEqual(view["status"], "failed")
        self.assertIn("לא עודכנו: חלב", view["result_text"])
        self.assertIn("עודכנה חלקית", view["result_text"])

    def test_cart_changed_line_untouched(self):
        self.search()
        r = sh.propose_batch([self.line("900101", 3, "חלב")], "run1")
        self.lines[0]["qty"] = 5                                 # Ben edited the cart in the app meanwhile
        results = []
        sh._batch_listeners[:] = [lambda run_id, res: results.append((run_id, res))]
        self.decide(r["approval_id"])
        res = self.wait_batch(results)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["changed"][0]["name"], "חלב")
        self.assertEqual(self.posts()[1:], [])                   # only the search POST

    def test_login_expired_before_writes(self):
        self.search()
        r = sh.propose_batch([self.line("900404", 1, "חטיף")], "run1")
        self.logged_in = False
        results = []
        sh._batch_listeners[:] = [lambda run_id, res: results.append((run_id, res))]
        self.decide(r["approval_id"])
        res = self.wait_batch(results)
        self.assertEqual(res["status"], "login_required")
        self.assertEqual(len(res["not_sent"]), 1)

    def test_blocked_mid_batch_stops(self):
        self.search()
        r = sh.propose_batch([self.line("900404", 2, "חטיף"), self.line("900101", 3, "חלב")], "run1")
        results = []
        sh._batch_listeners[:] = [lambda run_id, res: results.append((run_id, res))]
        self.override["checkout/cart/add"] = (429, {}, "")
        self.decide(r["approval_id"])
        res = self.wait_batch(results)
        self.assertEqual(res["status"], "failed", res)
        self.assertEqual([x["name"] for x in res["not_sent"]], ["חלב"])
        self.assertFalse([c for c in self.posts() if c[1] == "checkout/cart/edit"])

    def test_invalid_payload_refused(self):
        with self.assertRaises(ValueError):
            sh.apply_batch({"changes": [{"op": "remove", "product_id": "1", "qty_before": 1, "qty_after": 0}]})
        with self.assertRaises(ValueError):
            sh.apply_batch({"changes": [{"op": "add", "product_id": "1", "qty_before": 2, "qty_after": 1}]})

    # ---- the one automatic login of a weekly run (all mocked: no SMS, no site, fake clock)
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

    def test_gate_auto_login_success_via_relay(self):
        self.mock_login(relay_after=3)
        t0 = sh._now()
        g = sh.grocery_gate()
        self.assertEqual((g["status"], g["login"]), ("logged_in", "auto"))
        self.assertEqual(len(self.starts), 1)                    # one SMS
        self.assertLessEqual(sh._now() - t0, sh.LOGIN_BUDGET)

    def test_gate_auto_login_success_inside_start(self):
        self.mock_login(start_status="logged_in")
        g = sh.grocery_gate()
        self.assertEqual((g["status"], g["login"]), ("logged_in", "auto"))
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.relay_polls, [])

    def test_gate_ben_pastes_code_meanwhile(self):
        self.mock_login(relay_after=None)
        real_sleep = sh._sleep

        def sleep(sec):                                           # super_hist_login_code from chat
            real_sleep(sec)
            if len(self.slept) == 2:
                sh._state.update(phase="logged_in", logged_in_at=sh._now())
        sh._sleep = sleep
        self.assertEqual(sh.grocery_gate()["status"], "logged_in")
        self.assertEqual(len(self.starts), 1)

    def test_gate_auto_login_timeout_bounded(self):
        self.mock_login(relay_after=None)
        t0 = sh._now()
        g = sh.grocery_gate()
        self.assertEqual((g["status"], g["login"]), ("login_failed", "failed"))
        self.assertIn("3 דק׳", g["text"])
        self.assertEqual(len(self.starts), 1)                    # no second SMS, no retry
        self.assertLessEqual(sh._now() - t0, sh.LOGIN_BUDGET + 1)
        self.assertGreaterEqual(sh._now() - t0, sh.LOGIN_BUDGET - sh.LOGIN_POLL)
        self.assertEqual(len(self.starts), 1)

    def test_gate_auto_login_failure_no_retry(self):
        for status in ("rate_limited", "not_configured", "site_changed", "rejected"):
            with self.subTest(status=status):
                self.mock_login(start_status=status)
                g = sh.grocery_gate()
                self.assertEqual(g["status"], "login_failed")
                self.assertEqual(len(self.starts), 1)
                self.assertEqual(self.relay_polls, [])

    def test_gate_wrong_relayed_codes_end_the_attempt(self):
        self.mock_login(relay_after=1, relay_status="too_many_attempts")
        g = sh.grocery_gate()
        self.assertEqual(g["status"], "login_failed")
        self.assertIn("3 פעמים", g["text"])
        self.assertEqual(len(self.starts), 1)

    def test_gate_pending_code_is_waited_for_not_resent(self):
        self.mock_login(relay_after=2)
        sh._state.update(phase="code_sent", sent_at=sh._now(), started_at=sh._now())
        self.assertEqual(sh.grocery_gate()["status"], "logged_in")
        self.assertEqual(self.starts, [])                        # an SMS already went out: never a second one

    def test_gate_logged_in_no_login(self):
        self.mock_login()
        self.logged_in = True
        g = sh.grocery_gate()
        self.assertEqual((g["status"], g["login"]), ("logged_in", "none"))
        self.assertEqual(self.starts, [])

    def test_transport_maps_errors(self):
        self.assertTrue(sh.grocery_transport("חטיף"))
        self.logged_in = False
        sh._state["checked_at"] = 0.0
        with self.assertRaises(SessionExpired):
            sh.grocery_transport("חטיף")


if __name__ == "__main__":
    unittest.main()
