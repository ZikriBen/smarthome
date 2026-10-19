"""Command Center grocery routes + cart job against the vendored POC, offline (real NUC fixtures with barcodes)."""
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
os.environ.setdefault("GROCERY_LIST_BACKEND", "local")  # Tasks paths: test_grocery_tasks.py
FIXTURES = str(Path(__file__).parent / "fixtures" / "grocery-live")
if "command_center.database" not in sys.modules:
    fake_db = types.ModuleType("command_center.database")
    fake_db.connection = sqlite3.connect(":memory:", check_same_thread=False)
    sys.modules["command_center.database"] = fake_db
sys.path.insert(0, str(HERE))
from command_center import groceries as g  # noqa: E402
from command_center import grocery_db as db  # noqa: E402
from command_center import grocery_list as gl  # noqa: E402

ALL13 = ("ביצים, פלפל אדום, 5 מלפפון, חלב, ממרח שוקולד, ממרח חלבה, אבקת מרק עוף, פיתות, חומץ, רדיד אלומיניום, "
         "שיבולת שועל, קולה זירו, סוכר לבן")


def conn():
    return sys.modules["command_center.database"].connection


class GroceryRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        g.IMAGE_DEFAULTS = HERE / "grocery-defaults.yaml"
        g.DATA_DIR, g.STATE_DIR, g.REPORTS_DIR = root / "data", root / "data/state", root / "ws"
        g.DEFAULTS, g.USER_DEFAULTS = g.DATA_DIR / "defaults.yaml", g.DATA_DIR / "defaults.user.yaml"
        g.FIXTURES = FIXTURES
        g.setup()
        gl.configure(g.DEFAULTS, g.STATE_DIR / "current_list.json", "local")
        for t in ("grocery_site_state", "grocery_run_lines", "grocery_runs"):
            conn().execute(f"DELETE FROM {t}")
        g.POLL_SECONDS = 0.1
        conn().commit()
        self.api = g._api()
        self.real_cart = self.api.cart

    def tearDown(self):
        self.api.cart = self.real_cart
        g.clear({"undo": False}, {})
        g.FIXTURES = None
        self.tmp.cleanup()

    def wait(self, started, secs=30):
        return g.cart_status({"run_id": started["run_id"], "wait_seconds": secs}, {})

    def test_list_learn_audit_and_persistent_paths(self):
        self.assertIn("נוסף", g.add_item({"text": "חלב, 2 מלפפון"}, {})["text"])
        self.assertIn("מלפפון", g.list_items({}, {})["text"])
        self.assertIn("נלמד", g.learn({"item": "נייר כסף", "change": {"exclude": ["נצמד"]},
                                       "reason": "לא נייר נצמד"}, {})["text"])
        row = conn().execute("SELECT item,reason FROM grocery_learned WHERE item=?",
                             ("נייר כסף",)).fetchone()
        self.assertEqual(row, ("נייר כסף", "לא נייר נצמד"))
        self.assertIn("דמי משלוח", g.learn({"item": "משלוח",
            "change": {"delivery": {"rami_levy": {"fee": 29.90}}}, "reason": "מועדון"}, {})["text"])
        self.assertIn("29.9", g.USER_DEFAULTS.read_text(encoding="utf-8"))
        self.assertTrue(g.USER_DEFAULTS.is_file())
        g.setup()  # restart: base defaults refreshed, learned file kept
        self.assertTrue(g.USER_DEFAULTS.is_file())

    def test_max_qty_merge(self):
        g.add_item({"text": "חלב"}, {})
        self.assertIn("עודכן", g.add_item({"text": "2 חלב"}, {})["text"])
        self.assertIn("כבר ברשימה", g.add_item({"text": "חלב"}, {})["text"])
        self.assertIn("כבר ברשימה", g.add_item({"text": "1 חלב"}, {})["text"])
        self.assertIn("×2", g.list_items({}, {})["text"])

    def test_cart_end_to_end_both_links_and_history(self):
        g.add_item({"text": ALL13}, {})
        started = g.start_cart({}, {})
        self.assertEqual((started["status"], started["eta_seconds"], started["items"]), ("running", 180, 13))
        st = self.wait(started)
        self.assertEqual(st["status"], "done", st)
        links = st["links"]
        self.assertTrue(links["rami_levy"].startswith("https://www.rami-levy.co.il/he/basket?items="))
        self.assertTrue(links["shufersal"].startswith("https://www.shufersal.co.il/online/he/affiliate?items="))
        urls_in_msg = re.findall(r"\]\((https://[^)]+)\)", st["message"])
        self.assertEqual(urls_in_msg, [links["rami_levy"], links["shufersal"]])   # exact, unmodified
        self.assertIn("🛒 13 פריטים", st["message"])
        self.assertIn(";101,0.75;", links["rami_levy"])          # weighed: exact kg (5 × 0.15)
        self.assertNotIn("בדוק בעגלה", st["message"])
        self.assertIn("+ משלוח ₪35.90 = ₪", st["message"])       # delivery fee in final total
        self.assertRegex(st["message"], r"רמי לוי: ₪\d+\.\d{2} \+ משלוח")
        lines = db.run_lines(conn(), started["run_id"])
        self.assertEqual(len(lines), 26)
        self.assertEqual(sum(l["in_link"] for l in lines if l["site"] == "rami_levy"), 13)
        self.assertEqual(db.last_pick(conn(), "חלב", "rami_levy"), "419939")
        ok = conn().execute("SELECT site FROM grocery_site_state WHERE last_ok IS NOT NULL").fetchall()
        self.assertEqual(sorted(r[0] for r in ok), ["rami_levy", "shufersal"])

    def test_pin_last_pick_and_clear_marks_ordered(self):
        g.add_item({"text": "חלב"}, {})
        st = self.wait(g.start_cart({"sites": "rami_levy"}, {}))
        self.assertEqual(st["status"], "done", st)
        out = g.learn({"item": "חלב", "change": {"pin": {"rami_levy": ""}}, "reason": "תמיד את זה"}, {})
        self.assertIn("נלמד", out["text"])
        self.assertIn("419939", g.USER_DEFAULTS.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            g.learn({"item": "חלב", "change": {"pin": {"shufersal": ""}}}, {})
        g.clear({}, {})
        self.assertEqual(conn().execute("SELECT ordered FROM grocery_runs WHERE id=?",
                                        (st["run_id"],)).fetchone()[0], 1)

    def test_single_job_lock(self):
        gate = threading.Event()

        def slow(sites, **kw):
            gate.wait(5)
            return {"message": "m", "links": {}, "lines": {}, "site_errors": {}, "blocked": {},
                    "plan_paths": {}, "items": 1}
        self.api.cart = slow
        g.add_item({"text": "חלב"}, {})
        started = g.start_cart({}, {})
        with self.assertRaises(ValueError):
            g.start_cart({}, {})
        gate.set()
        self.assertEqual(self.wait(started)["status"], "done")
        self.assertEqual(self.wait(g.start_cart({}, {}))["status"], "done")   # lock released

    def test_circuit_breaker_block_skip_expire(self):
        calls = []

        def fake(sites, **kw):
            calls.append(dict(kw.get("skipped") or {}))
            blocked = {"shufersal": "HTTP 403 challenge"} if len(calls) == 1 else {}
            return {"message": "m", "links": {"rami_levy": "u", "shufersal": None}, "lines": {},
                    "site_errors": dict(blocked), "blocked": blocked, "plan_paths": {"rami_levy": "p"}, "items": 1}
        self.api.cart = fake
        g.add_item({"text": "חלב"}, {})
        self.wait(g.start_cart({}, {}))
        self.assertIsNotNone(db.blocked(conn(), "shufersal"))
        second = g.start_cart({}, {})
        self.assertIn("shufersal", second["skipped"])
        self.wait(second)
        self.assertIn("shufersal", calls[1])
        self.assertIsNone(db.blocked(conn(), "shufersal", now=time.time() + db.BLOCK_SECONDS + 1))
        conn().execute("UPDATE grocery_site_state SET blocked_until=0")
        conn().commit()
        self.wait(g.start_cart({}, {}))
        self.assertEqual(calls[2], {})

    def test_all_sites_blocked_eta_zero(self):
        db.set_blocked(conn(), "rami_levy", "x")
        g.add_item({"text": "חלב"}, {})
        started = g.start_cart({"sites": "rami_levy"}, {})
        self.assertEqual(started["eta_seconds"], 0)
        st = self.wait(started)
        self.assertIn("לא חיפשתי באף אתר", st["message"])

    def test_failed_job_recorded_and_restart_marks_running_failed(self):
        self.api.cart = lambda sites, **kw: (_ for _ in ()).throw(RuntimeError("site down"))
        g.add_item({"text": "חלב"}, {})
        st = self.wait(g.start_cart({}, {}))
        self.assertEqual(st["status"], "failed")
        self.assertIn("site down", st["error"])
        db.start_run(conn(), "zombie", ["rami_levy"])
        db.init(conn())
        self.assertEqual(db.get_run(conn(), "zombie")["status"], "failed")

    def test_empty_list_and_bad_site(self):
        r = g.start_cart({}, {})
        self.assertIsNone(r["run_id"])
        self.assertIn("ריקה", r["message"])
        with self.assertRaises(ValueError):
            g.start_cart({"sites": "amazon"}, {})
        with self.assertRaises(ValueError):
            g.cart_status({"run_id": "nope"}, {})



    def test_hermes_log_strings_19oct_never_400(self):
        # exact calls from the Hermes log (13:55 / 19:22 UTC)
        r = g.learn({"item": "ממרח חלווה", "change": "בנפרד", "reason": "ממרח חלווה בנפרד"}, {})
        self.assertIn("פיצול", r["text"])
        r = g.learn({"item": "כללי", "change": "תמיד מהדרין/בדצ",
                     "reason": "בכללי תוסיף לההעדפות, תמיד מהדרין/בדצ"}, {})
        self.assertTrue(r["text"].startswith("נלמד (כללי"), r)
        r = g.learn({"item": "כללי", "change": {"require_any": ["מהדרין", 'בד"צ', "בדצ"]}}, {})
        self.assertTrue(r["text"].startswith("נלמד (כללי"), r)
        r = g.learn({"item": "כללי", "change": "מהדרין"}, {})
        self.assertTrue(r["text"].startswith("נלמד (כללי"), r)
        r = g.learn({"item": "ביצים", "change": "שיהיו מתרנגולות חופש אם אפשר"}, {})
        self.assertTrue(r["text"].startswith("נשמר כהערה"), r)
        listed = g.preferences({"action": "list"}, {})["text"]
        self.assertIn("כללי (כל המוצרים): כשרות מהדרין", listed)
        self.assertIn("לא מיושם אוטומטית", listed)
        n = conn().execute("SELECT count(*) FROM grocery_learned").fetchone()[0]
        self.assertGreaterEqual(n, 4)          # 3 global + 1 note logged (the split reply is not a change)

    def test_add_splits_known_pair(self):
        out = g.add_item({"text": "ממרח שוקולד וממרח חלווה"}, {})["text"]
        self.assertIn("ממרח שוקולד", out)
        self.assertIn("ממרח חלבה", out)
        self.assertIn("(ברשימה: 2)", out)

    def test_preferences_list_and_parve_learn(self):
        self.assertIn("אין עדיין", g.preferences({"action": "list"}, {})["text"])
        self.assertIn("נלמד", g.learn({"item": "ממרח שוקולד", "change": "פרווה",
                                       "reason": "אני מעדיף פרווה"}, {})["text"])
        text = g.preferences({"action": "list"}, {})["text"]
        self.assertIn("ממרח שוקולד: חייב להכיל פרווה", text)
        self.assertIn("בלי חלבי", text)
        # pick on fixtures respects it
        from grocery.defaults import load_defaults
        from grocery.picker import Picker
        from grocery.sites.rami_levy import RamiLevyAdapter
        from grocery.http import FixtureSearcher
        d = load_defaults(str(g.DEFAULTS), str(g.USER_DEFAULTS))
        ad = RamiLevyAdapter()
        ad.searcher = FixtureSearcher(str(Path(FIXTURES) / "rami_levy"), lambda m: None, results_key="data")
        pick = Picker(ad, produce_defaults=d.produce).pick(d.resolve("ממרח שוקולד"))
        self.assertIn("פרווה", pick.chosen.p.name)
        self.assertIn("נמחקה", g.preferences({"action": "delete", "index": 2}, {})["text"])
        self.assertIn("שוחזרה", g.preferences({"action": "delete", "undo": True}, {})["text"])

class VendoredGuardTests(unittest.TestCase):
    def test_no_cart_write_or_login_code_vendored(self):
        root = HERE / "grocery"
        src = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in root.rglob("*.py")}
        self.assertNotIn("rami_login.py", src)
        for name, text in src.items():
            self.assertNotIn('"POST"', text, name)
        from grocery.sites.rami_levy import RamiLevyAdapter
        from grocery.sites.shufersal import ShufersalAdapter
        for cls in (RamiLevyAdapter, ShufersalAdapter):
            with self.assertRaises(NotImplementedError):
                cls().cart_merge_post(None, None, {"1": 1})
            with self.assertRaises(NotImplementedError):
                cls().load_session("/nonexistent")


class McpSchemaTests(unittest.TestCase):
    def test_exactly_eight_grocery_tools_register_in_fastmcp(self):
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError:
            self.skipTest("mcp not installed")
        from command_center.mcp_tools import register_all
        mcp = FastMCP("t")
        register_all(mcp, object())
        tools = asyncio.run(mcp.list_tools())
        names = sorted(t.name for t in tools if t.name.startswith("grocery_"))
        self.assertEqual(names, sorted(["grocery_add_item", "grocery_list", "grocery_remove", "grocery_clear",
                                        "grocery_learn_preference", "grocery_preferences",
                                        "grocery_cart", "grocery_cart_status"]))
        cfg = (HERE.parent / "hermes" / "config.yaml").read_text(encoding="utf-8")
        listed = sorted(set(re.findall(r"^\s*- (grocery_\w+)\s*$", cfg, re.M)))
        self.assertEqual(listed, names)


if __name__ == "__main__":
    unittest.main()
