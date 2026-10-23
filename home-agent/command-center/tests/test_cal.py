"""Command Center Cal card sync: propose/approve/status/limits/archive reads. Offline, synthetic, no Cal."""
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
HERE = Path(__file__).resolve().parents[1]


def load_cal(spool, archive_dir):
    calls = []
    approvals = types.ModuleType("approvals")
    approvals.create = lambda action, payload: calls.append((action, payload)) or "appr-cal"
    approvals.register_action = lambda action, fn: calls.append(("register", action))
    spec = importlib.util.spec_from_file_location("cal_archive", HERE / "command_center" / "cal_archive.py")
    amod = importlib.util.module_from_spec(spec)
    sys.modules["cal_archive"] = amod
    spec.loader.exec_module(amod)
    sys.modules["approvals"] = approvals
    source = (HERE / "command_center" / "cal.py").read_text()
    source = source.replace("from . import approvals", "import approvals")
    source = source.replace("from . import cal_archive as archive", "import cal_archive as archive")
    module = types.ModuleType("cal_under_test")
    exec(compile(source, "cal.py", "exec"), module.__dict__)
    module.SPOOL_DIR = spool
    module.ARCHIVE_DIR = archive_dir
    module._test_calls = calls
    module._archive = amod
    return module


class CalCCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.spool = root / "spool"
        self.spool.mkdir()
        self.arch = root / "archive"
        self.cal = load_cal(self.spool, self.arch)

    def tearDown(self):
        sys.modules.pop("approvals", None)
        self.tmp.cleanup()

    def runs(self, runs, max_runs=2, date=None):
        (self.spool / "runs-today.json").write_text(json.dumps(
            {"date": date or self.cal._today(), "runs": runs, "max_runs": max_runs, "updated_at": time.time()}))

    def test_propose_default_and_execute(self):
        out = self.cal.propose_sync({})
        self.assertEqual(out["status"], "pending_approval", out)
        self.assertEqual(self.cal._test_calls[0][0], "card_sync")
        payload = self.cal._test_calls[0][1]
        self.assertEqual(payload["actions"][0]["name"], "card_transactions")
        self.assertIn("כאל", out["text"])
        res = self.cal.execute_sync(payload)
        self.assertTrue(res["queued"])
        req = json.loads((self.spool / "request.json").read_text())
        self.assertEqual(req["actions"][0]["name"], "card_transactions")
        self.assertEqual(self.cal.propose_sync({})["status"], "already_pending")

    def test_execute_rejects_foreign_actions(self):
        with self.assertRaises(ValueError):
            self.cal.execute_sync({"actions": [{"name": "checking_transactions"}]})
        self.assertFalse((self.spool / "request.json").exists())

    def test_bad_months(self):
        self.assertEqual(self.cal.propose_sync({"from_month": "2026-13"})["status"], "bad_request")
        self.assertEqual(self.cal.propose_sync({"from_month": "2026-09", "to_month": "2026-08"})["status"], "bad_request")

    def test_daily_limit_refuses_before_approval_and_spool(self):
        self.runs(2)
        out = self.cal.propose_sync({})
        self.assertEqual(out["status"], "DAILY_LIMIT")
        self.assertEqual((out["runs_today"], out["max_runs"]), (2, 2))
        self.assertEqual(self.cal._test_calls, [])
        with self.assertRaises(ValueError):
            self.cal.execute_sync({"actions": [{"name": "card_transactions"}]})
        self.assertFalse((self.spool / "request.json").exists())
        st = self.cal.status()
        self.assertFalse(st["can_run_today"])
        self.assertIn("מכסת", st["error_text"])

    def test_limit_uses_runner_max_and_ignores_yesterday(self):
        self.runs(2, max_runs=5)
        self.assertEqual(self.cal.propose_sync({})["status"], "pending_approval")
        self.runs(9, max_runs=2, date="2000-01-01")
        self.assertFalse(self.cal._daily_limit()["limit_reached"])
        (self.spool / "runs-today.json").write_text("{broken")
        self.assertTrue(self.cal._daily_limit()["limit_reached"])   # fail closed today

    def test_stopped_today(self):
        (self.spool / "last-status.json").write_text(json.dumps(
            {"status": "INVALID_PASSWORD", "stopForToday": True, "finishedAt": time.time()}))
        out = self.cal.propose_sync({})
        self.assertEqual(out["status"], "stopped_today")
        self.assertIn("כאל", out["text"])

    def test_covered_needs_no_approval(self):
        a = self.cal._archive
        a.merge_sync(self.arch, {"cards": ["1111"], "retrieval": {"1111": {"known": True, "months": [
            {"month": "2026-02", "rows": 1, "capped": False}], "pending": "skipped"}},
            "rows": [{"card": "1111", "billing_month": "2026-02", "purchase_date": "2026-01-05",
                      "charge_date": "2026-02-10", "amount": -5, "description": "synthetic", "identifier": "x"}]})
        out = self.cal.propose_sync({"from_month": "2026-02", "to_month": "2026-02"})
        self.assertEqual(out["status"], "COVERED")
        self.assertEqual(self.cal._test_calls, [])
        m = self.cal.read_month({"month": "2026-02"})
        self.assertEqual(m["row_count"], 1)
        self.assertTrue(m["complete"])
        r = self.cal.read_range({"from": "2026-02-01", "to": "2026-02-28"})
        self.assertEqual(r["row_count"], 1)
        self.assertEqual(self.cal.read_range({"from": "x", "to": "y"})["status"], "bad_request")
        self.assertEqual(self.cal.read_month({"month": "2026-02", "card": "12"})["status"], "bad_request")
        out = self.cal.propose_sync({"from_month": "2026-02", "to_month": "2026-03"})
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(out["fetch_from"], "2026-03")

    def test_status_allowlist(self):
        (self.spool / "last-status.json").write_text(json.dumps(
            {"status": "DONE", "cardCount": 2, "secret": "x", "detail": "y", "finishedAt": time.time()}))
        st = self.cal.status()
        self.assertEqual(st["last_run"]["cardCount"], 2)
        self.assertNotIn("secret", st["last_run"])
        self.assertNotIn("detail", st["last_run"])
        self.assertTrue(st["can_run_today"])

    def test_error_texts_cover_runner_stop_statuses(self):
        spec = importlib.util.spec_from_file_location(
            "cal_runner_for_cc", HERE.parent / "hapoalim-sync" / "host" / "cal-runner.py")
        sys.path.insert(0, str(HERE.parent / "hapoalim-sync" / "host"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        missing = {s for s in mod.CAL_STOP if s not in self.cal.ERROR_TEXT_HE} - {
            "ACTION_ERROR", "RUN_TIMEOUT", "PROTOCOL_ERROR", "INTERNAL"}
        self.assertEqual(missing, set())
        self.assertTrue(set(mod.CAL_FIELDS) <= set(self.cal.STATUS_FIELDS))

    def test_mcp_tool_names(self):
        src = (HERE / "command_center" / "mcp_tools" / "cal.py").read_text()
        for name in ("card_status", "card_sync", "card_read_month", "card_read_range"):
            self.assertIn(f"def {name}(", src)

    def test_approval_text(self):
        src = (HERE / "command_center" / "approvals.py").read_text()
        self.assertIn('action == "card_sync"', src)


if __name__ == "__main__":
    unittest.main()
