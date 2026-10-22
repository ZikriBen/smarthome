"""Command Center Hapoalim sync: propose/approve/status/read-csv. Offline, no bank."""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
HERE = Path(__file__).resolve().parents[1]


def load_hapoalim(spool, out):
    """Load hapoalim.py with a stub approvals module (no /data sqlite)."""
    calls = []
    approvals = types.ModuleType("approvals")
    approvals.create = lambda action, payload: calls.append((action, payload)) or "appr01"
    approvals.register_action = lambda action, fn: None

    source = (HERE / "command_center" / "hapoalim.py").read_text()
    source = source.replace("from . import approvals", "import approvals")
    source = source.replace("from . import hapoalim_archive as archive", "import hapoalim_archive as archive")
    aspec = importlib.util.spec_from_file_location(
        "hapoalim_archive", HERE / "command_center" / "hapoalim_archive.py")
    amod = importlib.util.module_from_spec(aspec)
    sys.modules["hapoalim_archive"] = amod
    aspec.loader.exec_module(amod)
    module = types.ModuleType("hapoalim_under_test")
    module.approvals = approvals
    # Make `import approvals` resolve to our stub when the source runs.
    sys.modules["approvals"] = approvals
    exec(compile(source, "hapoalim.py", "exec"), module.__dict__)
    module.SPOOL_DIR = spool
    module.OUT_DIR = out
    module.ARCHIVE_DIR = out.parent / "archive"
    module.ARCHIVE_DIR.mkdir(exist_ok=True)
    module.REPULL_DAYS = 0
    module._test_calls = calls
    return module


class HapoalimCCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.spool = root / "spool"
        self.out = root / "out"
        self.spool.mkdir()
        self.out.mkdir()
        self.hp = load_hapoalim(self.spool, self.out)
        self.archive = self.hp.ARCHIVE_DIR

    def tearDown(self):
        sys.modules.pop("approvals", None)
        self.tmp.cleanup()

    def test_propose_default_and_approve(self):
        out = self.hp.propose_sync({})
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(out["approval_id"], "appr01")
        self.assertEqual(self.hp._test_calls[0][0], "hapoalim_sync")
        result = self.hp.execute_sync({"actions": [{"name": "checking_transactions", "from": "2026-09-01"}]})
        self.assertTrue(result["queued"])
        req = json.loads((self.spool / "request.json").read_text())
        self.assertEqual(req["actions"][0]["name"], "checking_transactions")
        self.assertIn("requested_at", req)

    def test_bad_dates(self):
        out = self.hp.propose_sync({"from": "nope", "to": "2026-10-01"})
        self.assertEqual(out["status"], "bad_request")

    def test_read_csv(self):
        name = "hapoalim-20261022-120000-checking_transactions.csv"
        (self.out / name).write_text("date,description,amount\n2026-10-01,סופר,-50\n", encoding="utf-8")
        (self.spool / "last-status.json").write_text(json.dumps({"status": "DONE", "csv": name}))
        out = self.hp.read_csv({})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["row_count"], 1)
        self.assertEqual(out["rows"][0]["description"], "סופר")

    def test_stopped_today_refuses_propose(self):
        import time
        (self.spool / "last-status.json").write_text(json.dumps({
            "status": "OTP_TIMEOUT", "stopForToday": True, "finishedAt": time.time()}))
        out = self.hp.propose_sync({})
        self.assertEqual(out["status"], "stopped_today")



    def test_covered_no_approval(self):
        arch = sys.modules["hapoalim_archive"]
        arch.merge_sync(
            self.archive,
            [{"account": "1234", "date": "2026-09-15", "amount": -1, "description": "z",
              "identifier": "1", "status": "completed", "processed_date": "2026-09-15"}],
            range_from="2026-09-01", range_to="2026-09-30", run_id="seed",
        )
        out = self.hp.propose_sync({"from": "2026-09-01", "to": "2026-09-30"})
        self.assertEqual(out["status"], "COVERED")
        self.assertEqual(self.hp._test_calls, [])

    def test_backfill_clamp_warning(self):
        out = self.hp.propose_sync({"from": "2020-01-01", "to": "2026-10-01"})
        # Either COVERED (no) or pending_approval with clamp warning
        self.assertEqual(out["status"], "pending_approval")
        self.assertTrue(any("clamped" in w for w in (out.get("warnings") or [])))
        cap = out["fetch_from"]
        from datetime import date, datetime, timedelta
        from zoneinfo import ZoneInfo
        t = datetime.now(ZoneInfo("Asia/Jerusalem")).date()
        self.assertEqual(cap, (t.replace(year=t.year - 1) + timedelta(days=1)).isoformat())

    def test_list_archive_and_read_month(self):
        arch = sys.modules["hapoalim_archive"]
        arch.merge_sync(
            self.archive,
            [{"account": "9999", "date": "2026-10-05", "amount": -3, "description": "x",
              "identifier": "9", "status": "completed", "processed_date": "2026-10-05"}],
            range_from="2026-10-01", range_to="2026-10-31", run_id="seed",
        )
        listing = self.hp.list_archive()
        self.assertEqual(listing["status"], "ok")
        self.assertEqual(len(listing["files"]), 1)
        month = self.hp.read_month({"month": "2026-10", "account": "9999"})
        self.assertEqual(month["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
