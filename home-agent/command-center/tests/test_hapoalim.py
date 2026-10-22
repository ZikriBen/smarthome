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
    module = types.ModuleType("hapoalim_under_test")
    module.approvals = approvals
    # Make `import approvals` resolve to our stub when the source runs.
    sys.modules["approvals"] = approvals
    exec(compile(source, "hapoalim.py", "exec"), module.__dict__)
    module.SPOOL_DIR = spool
    module.OUT_DIR = out
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


if __name__ == "__main__":
    unittest.main()
