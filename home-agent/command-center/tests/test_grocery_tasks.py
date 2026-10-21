"""Grocery list with Google Tasks as the source of truth (fake Tasks API, no network)."""
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if "command_center.database" not in sys.modules:
    fake_db = types.ModuleType("command_center.database")
    fake_db.connection = sqlite3.connect(":memory:", check_same_thread=False)
    sys.modules["command_center.database"] = fake_db
from command_center import groceries as g  # noqa: E402
from command_center import grocery_list as gl  # noqa: E402
from command_center import tasks_store as ts  # noqa: E402
from fake_tasks import FakeHttpError, FakeTasksAPI  # noqa: E402

WARN = "⚠️ Google Tasks"


class GroceryTasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        g.IMAGE_DEFAULTS = HERE / "grocery-defaults.yaml"
        g.DATA_DIR, g.STATE_DIR, g.REPORTS_DIR = root / "data", root / "data/state", root / "ws"
        g.DEFAULTS, g.USER_DEFAULTS = g.DATA_DIR / "defaults.yaml", g.DATA_DIR / "defaults.user.yaml"
        g.setup()
        self.api = FakeTasksAPI(page_size=2)
        self.state = g.STATE_DIR / "current_list.json"
        gl.configure(g.DEFAULTS, self.state, "tasks",
                     store_factory=lambda: ts.TasksStore(service=self.api, list_title="קניות"))

    def tearDown(self):
        self.tmp.cleanup()

    def say(self, fn, **p):
        return fn(p, {})["text"] if not isinstance(fn(p, {}), str) else fn(p, {})

    def cache(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def run_items(self):
        items, _ = g._api().current_items(str(g.DEFAULTS))
        return sorted(i.name for i in items)

    def test_add_creates_list_and_tasks(self):
        out = g.add_item({"text": "חלב, 2 מלפפון"}, {})["text"]
        self.assertNotIn(WARN, out)
        self.assertEqual(self.api.open_titles(), ["חלב", "מלפפון ×2"])
        self.assertEqual(self.api.inserted_lists, 1)
        self.assertTrue(all(e["task_ids"] for e in self.cache()["items"]))
        self.assertIn("כבר ברשימה", g.add_item({"text": "חלב"}, {})["text"])
        self.assertEqual(len(self.api.open_titles()), 2)

    def test_phone_edits_win_and_ticked_items_skip_the_run(self):
        g.add_item({"text": "חלב, ביצים"}, {})
        self.api.phone_add("2 עגבניות")
        self.api.phone_tick("חלב")
        listed = g.list_items({}, {})["text"]
        self.assertIn("עגבני", listed)
        self.assertNotIn("חלב", listed)
        self.assertNotIn("חלב", self.run_items())
        self.assertEqual(len(self.run_items()), 2)

    def test_quantity_update_and_phone_conflict(self):
        g.add_item({"text": "2 מלפפון"}, {})
        self.assertIn("עודכן", g.add_item({"text": "3 מלפפון"}, {})["text"])
        self.assertEqual(self.api.open_titles(), ["מלפפון ×3"])
        tid = self.api.by_title("מלפפון ×3")["id"]
        self.api.edit_on_get.add(tid)              # Ben edits it between our read and write
        out = g.add_item({"text": "4 מלפפון"}, {})["text"]
        self.assertIn("השתנה בטלפון", out)
        self.assertEqual(self.api.open_titles(), ["מלפפון ×3 (phone)"])

    def test_remove_and_multi_item_task(self):
        g.add_item({"text": "פיתות"}, {})
        self.api.phone_add("חלב, ביצים")
        self.assertIn("הוסר", g.remove_item({"item": "פיתות"}, {})["text"])
        self.assertIn("הוסר", g.remove_item({"item": "ביצים"}, {})["text"])
        self.assertEqual(self.api.open_titles(), ["חלב"])
        self.assertIn("לא נמצא", g.remove_item({"item": "שוקולד"}, {})["text"])

    def test_clear_ticks_and_undo_restores(self):
        g.add_item({"text": "חלב, ביצים, סוכר"}, {})
        out = g.clear({}, {})["text"]
        self.assertIn("נוקתה (3", out)
        self.assertEqual(self.api.open_titles(), [])
        lid = self.api.list_id()
        self.assertEqual(sum(t["status"] == "completed" for t in self.api.store[lid].values()), 3)
        self.assertIn("שוחזרו 3", g.clear({"undo": True}, {})["text"])
        self.assertEqual(len(self.api.open_titles()), 3)
        self.assertIn("אין ניקוי", g.clear({"undo": True}, {})["text"])

    def test_offline_falls_back_to_cache_and_replays(self):
        g.add_item({"text": "חלב"}, {})
        self.api.fail = FakeHttpError(503)
        out = g.add_item({"text": "2 מלפפון"}, {})["text"]
        self.assertIn(WARN, out)
        self.assertIn("לא זמין", out)
        self.assertIn("מלפפון", g.list_items({}, {})["text"])        # served from the cache
        self.assertEqual(len(self.cache()["pending"]), 1)
        self.assertIn("1 שינויים ממתינים", g.sync({}, {})["text"])
        self.api.fail = None
        self.assertIn("הועלו 1", g.sync({}, {})["text"])
        self.assertEqual(self.api.open_titles(), ["חלב", "מלפפון ×2"])
        self.assertEqual(self.cache()["pending"], [])

    def test_offline_remove_of_queued_add_cancels_it(self):
        self.api.fail = FakeHttpError(503)
        g.add_item({"text": "קפה"}, {})
        g.remove_item({"item": "קפה"}, {})
        self.assertEqual(self.cache()["pending"], [])

    def test_missing_scope_says_reconsent_and_never_raises(self):
        self.api.fail = FakeHttpError(403, "Request had insufficient authentication scopes.")
        out = g.list_items({}, {})["text"]
        self.assertIn("re-consent", out)

    def test_real_factory_without_token_is_a_clear_message(self):
        gl.configure(g.DEFAULTS, self.state, "tasks")   # real TasksStore -> tasks_credentials()
        with mock.patch.object(ts, "GOOGLE_TASKS_TOKEN", Path(self.tmp.name) / "none.json"):
            out = g.add_item({"text": "חלב"}, {})["text"]
        self.assertIn("נוסף", out)
        self.assertIn("re-consent", out)

    def test_cart_syncs_first_and_carries_the_warning(self):
        seen = {}
        api = g._api()
        real = api.cart

        def fake_cart(sites, **kw):
            seen["items"] = self.run_items()
            return {"message": "🛒 רמי לוי ₪1", "links": {"rami_levy": "https://x/basket?items=1,1"},
                    "lines": {}, "site_errors": {}, "blocked": {}, "plan_paths": {}, "items": 1}
        try:
            api.cart = fake_cart
            self.api.new_list("קניות")
            self.api.phone_add("חלב")
            self.api.phone_add("סוכר")
            self.api.phone_tick("סוכר")
            started = g.start_cart({"sites": "rami_levy"}, {})
            self.assertNotIn("warning", started)
            st = g.cart_status({"run_id": started["run_id"], "wait_seconds": 10}, {})
            self.assertEqual(st["status"], "done", st)
            self.assertEqual(seen["items"], ["חלב"])
            self.api.fail = FakeHttpError(503)
            started = g.start_cart({"sites": "rami_levy"}, {})
            self.assertIn(WARN, started["warning"])
            st = g.cart_status({"run_id": started["run_id"], "wait_seconds": 10}, {})
            self.assertTrue(st["message"].startswith(WARN), st)
        finally:
            api.cart = real

    def test_old_cache_format_still_loads(self):
        self.state.write_text(json.dumps({"items": [
            {"text": "חלב", "item": "חלב", "qty": None, "qty_unit": None, "known": True}]}),
            encoding="utf-8")
        self.api.fail = FakeHttpError(503)
        self.assertIn("1 פריטים", g.list_items({}, {})["text"])

    def test_mcp_tools_registered(self):
        from command_center.mcp_tools import groceries as tools
        names = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    names[fn.__name__] = fn
                    return fn
                return deco
        sent = []

        class Client:
            def request(self, method, path, payload=None, timeout=30):
                sent.append((path, payload))
                return {"text": "ok"}
        tools.register(FakeMCP(), Client())
        self.assertEqual(sorted(names), sorted([
            "grocery_add_item", "grocery_list", "grocery_remove", "grocery_clear",
            "grocery_learn_preference", "grocery_preferences", "grocery_cart", "grocery_cart_status"]))
        names["grocery_clear"](undo=True)
        names["grocery_cart"]()
        names["grocery_cart_status"]("abc", wait_seconds=500)
        self.assertEqual(sent, [("/grocery/clear", {"undo": True}), ("/grocery/cart", {"sites": "weekly"}),
                                ("/grocery/cart-status", {"run_id": "abc", "wait_seconds": 90})])
        for fn in names.values():
            self.assertLess(len(fn.__doc__), 600, fn.__name__)


if __name__ == "__main__":
    unittest.main()
