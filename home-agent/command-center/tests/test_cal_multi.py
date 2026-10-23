"""Command Center multi-account card sync: backward compatibility (no account == the single-account a1a5c34
outputs, on the old AND the migrated layout), routing, per-account limits/stop, global pending, unknown /
unsupported accounts, last-4 resolution and ambiguity. Offline, synthetic, no Cal."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
HERE = Path(__file__).resolve().parents[1]
OLD = HERE / "tests" / "fixtures" / "cal_single_account_a1a5c34.py"
NEW = HERE / "command_center" / "cal.py"
# Keys the multi-account version ADDS; everything else must be identical to the old output.
ADDED = {"account", "accounts", "provider", "label", "enabled", "supported", "migration_pending",
         "busy_with_account", "max_runs_total"}


def load(path, spool, archive_dir):
    calls = []
    approvals = types.ModuleType("approvals")
    approvals.create = lambda action, payload: calls.append((action, payload)) or "appr-cal"
    approvals.register_action = lambda action, fn: None
    sys.modules["approvals"] = approvals
    for name in ("cal_archive", "card_accounts"):
        spec = importlib.util.spec_from_file_location(name, HERE / "command_center" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    source = path.read_text()
    for a, b in (("from . import approvals", "import approvals"),
                 ("from . import cal_archive as archive", "import cal_archive as archive"),
                 ("from . import card_accounts as accounts", "import card_accounts as accounts")):
        source = source.replace(a, b)
    module = types.ModuleType(f"cal_{path.stem}")
    exec(compile(source, str(path), "exec"), module.__dict__)
    module.SPOOL_DIR, module.ARCHIVE_DIR, module._calls = spool, archive_dir, calls
    return module


def strip(obj):
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if k not in ADDED}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj


def payload(cards, months, tag="x"):
    return {"cards": cards, "retrieval": {c: {"known": True, "months": [{"month": m, "rows": 1, "capped": False}
                                                                       for m in months], "pending": "skipped"}
                                          for c in cards},
            "rows": [{"card": c, "billing_month": m, "purchase_date": f"{m}-05", "charge_date": f"{m}-10",
                      "amount": -5, "description": f"synthetic {tag}", "identifier": f"{tag}{c}{m}"}
                     for c in cards for m in months]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spool, self.arch = self.root / "spool", self.root / "archive"
        self.spool.mkdir()
        self.arch.mkdir()
        self.new = load(NEW, self.spool, self.arch)
        self.ar = sys.modules["cal_archive"]

    def tearDown(self):
        sys.modules.pop("approvals", None)
        self.tmp.cleanup()

    def mirror(self, *extra):
        accts = [{"id": "cal-main", "provider": "cal", "label": "Cal", "enabled": True, "supported": True,
                  "max_runs_per_day": 2},
                 {"id": "cal-2", "provider": "cal", "label": "Cal 2", "enabled": True, "supported": True,
                  "max_runs_per_day": 1}] + list(extra)
        (self.spool / "accounts.json").write_text(json.dumps({"version": 1, "default": "cal-main",
                                                              "max_runs_total": 0, "accounts": accts}))

    def legacy(self):
        self.ar.merge_sync(self.arch, payload(["1111", "2222"], ["2026-02", "2026-03"]))
        (self.spool / "last-status.json").write_text(json.dumps(
            {"status": "DONE", "cardCount": 2, "finishedAt": time.time(), "secret": "no"}))
        (self.spool / "runs-today.json").write_text(json.dumps(
            {"date": self.new._today(), "runs": 1, "max_runs": 2, "updated_at": time.time()}))

    def migrate(self):
        (self.arch / "cards" / "cal-main").mkdir(parents=True)
        for p in list(self.arch.iterdir()):
            if p.is_file() and (p.name.startswith("cal-card-") or p.name == "_coverage.json"):
                p.rename(self.arch / "cards" / "cal-main" / p.name)
        (self.spool / "accounts" / "cal-main").mkdir(parents=True)
        for n in ("last-status.json", "runs-today.json"):
            (self.spool / n).rename(self.spool / "accounts" / "cal-main" / n)

    def calls(self):
        return [
            ("status", lambda m: m.status({}, {})),
            ("list_archive", lambda m: m.list_archive({}, {})),
            ("coverage", lambda m: m.coverage({}, {})),
            ("read_month", lambda m: m.read_month({"month": "2026-02"})),
            ("read_month_card", lambda m: m.read_month({"month": "2026-03", "card": "2222"})),
            ("read_range", lambda m: m.read_range({"from": "2026-02-01", "to": "2026-03-31"})),
            ("read_range_purchase", lambda m: m.read_range({"from": "2026-02-01", "to": "2026-03-31", "by": "purchase"})),
            ("propose_covered", lambda m: m.propose_sync({"from_month": "2026-02", "to_month": "2026-03"})),
            ("propose_gap", lambda m: m.propose_sync({"from_month": "2026-02", "to_month": "2026-04"})),
        ]


class BackwardCompatTests(Base):
    def outputs(self, module):
        out = {}
        for name, fn in self.calls():
            r = fn(module)
            if isinstance(r, dict):
                r = {k: v for k, v in r.items() if k not in ("resets_at",)}
            out[name] = r
        return out

    def test_no_account_equals_single_account_outputs_on_old_layout(self):
        self.legacy()
        old = load(OLD, self.spool, self.arch)
        expected = self.outputs(old)
        self.new = load(NEW, self.spool, self.arch)
        got = self.outputs(self.new)
        for name in expected:
            self.assertEqual(strip(got[name]), expected[name], name)
        self.assertTrue(got["status"]["migration_pending"])
        self.assertEqual(got["read_month"]["account"], "cal-main")

    def test_no_account_equals_single_account_outputs_after_migration(self):
        self.legacy()
        old = load(OLD, self.spool, self.arch)
        expected = self.outputs(old)
        self.migrate()
        self.mirror()
        self.new = load(NEW, self.spool, self.arch)
        got = self.outputs(self.new)
        for name in expected:
            self.assertEqual(strip(got[name]), expected[name], name)
        self.assertNotIn("migration_pending", got["status"])
        self.assertEqual(sorted(got["status"]["accounts"]), ["cal-2", "cal-main"])
        self.assertEqual(sorted(got["coverage"]["accounts"]), ["cal-2", "cal-main"])

    def test_old_request_shape_still_queued_for_cal_main(self):
        self.legacy()
        self.new.propose_sync({})
        approved = self.new._calls[-1][1]
        self.assertEqual(approved["account"], "cal-main")
        approved.pop("account")                           # an approval created by the OLD code (no account)
        self.new.execute_sync(approved)
        req = json.loads((self.spool / "request.json").read_text())
        self.assertEqual(req["account"], "cal-main")


class RoutingTests(Base):
    def setUp(self):
        super().setUp()
        self.mirror()
        self.ar.merge_sync(self.arch / "cards" / "cal-main", payload(["1111", "2222"], ["2026-02"], "m"))
        self.ar.merge_sync(self.arch / "cards" / "cal-2", payload(["3333", "1111"], ["2026-02"], "b"))

    def test_status_grouped_and_single(self):
        st = self.new.status({}, {})
        self.assertEqual(st["account"], "cal-main")
        self.assertEqual(st["accounts"]["cal-2"]["label"], "Cal 2")
        self.assertEqual(st["accounts"]["cal-2"]["max_runs"], 1)
        one = self.new.status({"account": "cal-2"}, {})
        self.assertEqual(one["account"], "cal-2")
        self.assertNotIn("accounts", one)
        self.assertEqual(self.new.status({"account": "cal-9"})["status"], "UNKNOWN_ACCOUNT")
        self.assertEqual(self.new.status({"account": "Bad Id"})["status"], "BAD_ACCOUNT")

    def test_propose_and_execute_carry_the_account(self):
        out = self.new.propose_sync({"account": "cal-2", "from_month": "2026-02", "to_month": "2026-03"})
        self.assertEqual((out["status"], out["account"]), ("pending_approval", "cal-2"))
        self.assertIn("cal-2", out["text"])
        action, approved = self.new._calls[-1]
        self.assertEqual((action, approved["account"]), ("card_sync", "cal-2"))
        self.new.execute_sync(approved)
        self.assertEqual(json.loads((self.spool / "request.json").read_text())["account"], "cal-2")
        # one pending run blocks every account (one bank login at a time)
        busy = self.new.propose_sync({"account": "cal-main", "from_month": "2026-02", "to_month": "2026-03"})
        self.assertEqual((busy["status"], busy["busy_with_account"]), ("already_pending", "cal-2"))
        st = self.new.status({}, {})
        self.assertFalse(st["can_run_today"])
        self.assertEqual(st["busy_with_account"], "cal-2")
        self.assertEqual(st["accounts"]["cal-2"]["pending_run"]["stage"], "queued")

    def test_covered_per_account(self):
        self.assertEqual(self.new.propose_sync({"account": "cal-2", "from_month": "2026-02",
                                                "to_month": "2026-02"})["status"], "COVERED")

    def test_limits_and_stop_are_per_account(self):
        d = self.spool / "accounts" / "cal-main"
        d.mkdir(parents=True)
        (d / "runs-today.json").write_text(json.dumps({"date": self.new._today(), "runs": 2, "max_runs": 2}))
        (d / "last-status.json").write_text(json.dumps({"status": "INVALID_PASSWORD", "stopForToday": True,
                                                        "finishedAt": time.time()}))
        self.assertEqual(self.new.propose_sync({"from_month": "2026-02", "to_month": "2026-03"})["status"], "stopped_today")
        self.assertEqual(self.new.propose_sync({"account": "cal-2", "from_month": "2026-02", "to_month": "2026-03"})["status"],
                         "pending_approval")
        st = self.new.status({}, {})
        self.assertFalse(st["can_run_today"])
        self.assertTrue(st["accounts"]["cal-2"]["can_run_today"])
        # global cap published by the runner
        (self.spool / "runs-total.json").write_text(json.dumps({"date": self.new._today(), "runs_total": 3,
                                                                "max_runs_total": 3}))
        out = self.new.propose_sync({"account": "cal-2", "from_month": "2026-02", "to_month": "2026-03"})
        self.assertEqual((out["status"], out["limit_scope"]), ("DAILY_LIMIT", "global"))

    def test_unsupported_and_disabled(self):
        self.mirror({"id": "max-1", "provider": "max", "label": "Max", "enabled": True, "supported": False},
                    {"id": "cal-3", "provider": "cal", "label": "off", "enabled": False, "supported": True})
        out = self.new.propose_sync({"account": "max-1"})
        self.assertEqual(out["status"], "NOT_SUPPORTED")
        self.assertEqual(self.new.propose_sync({"account": "cal-3"})["status"], "ACCOUNT_DISABLED")
        self.assertEqual(self.new.propose_sync({"account": "cal-9"})["status"], "UNKNOWN_ACCOUNT")
        with self.assertRaises(ValueError):
            self.new.execute_sync({"actions": [{"name": "card_transactions"}], "account": "max-1"})
        self.assertFalse((self.spool / "request.json").exists())
        self.assertEqual(self.new._calls, [])

    def test_last4_resolution_and_ambiguity(self):
        r = self.new.read_month({"month": "2026-02", "card": "3333"})
        self.assertEqual((r["account"], r["row_count"]), ("cal-2", 1))
        r = self.new.read_month({"month": "2026-02", "card": "2222"})
        self.assertEqual(r["account"], "cal-main")
        amb = self.new.read_month({"month": "2026-02", "card": "1111"})
        self.assertEqual(amb["status"], "AMBIGUOUS_CARD")
        self.assertEqual(sorted(amb["accounts"]), ["cal-2", "cal-main"])
        amb = self.new.read_range({"from": "2026-02-01", "to": "2026-02-28", "card": "1111"})
        self.assertEqual(amb["status"], "AMBIGUOUS_CARD")
        r = self.new.read_range({"from": "2026-02-01", "to": "2026-02-28", "card": "1111", "account": "cal-2"})
        self.assertEqual((r["account"], r["row_count"]), ("cal-2", 1))
        r = self.new.read_month({"month": "2026-02", "account": "cal-2"})
        self.assertEqual(r["row_count"], 2)
        self.assertEqual(self.new.read_month({"month": "2026-02", "account": "nope!"})["status"], "BAD_ACCOUNT")

    def test_list_and_coverage_grouped(self):
        lst = self.new.list_archive({}, {})
        self.assertEqual(sorted(lst["accounts"]), ["cal-2", "cal-main"])
        self.assertTrue(all("cal-card-3333" not in f.get("name", "") for f in lst["files"]))
        one = self.new.list_archive({"account": "cal-2"}, {})
        self.assertNotIn("accounts", one)
        self.assertEqual(one["account"], "cal-2")
        cov = self.new.coverage({"account": "cal-2"}, {})
        self.assertEqual(cov["account"], "cal-2")

    def test_missing_mirror_means_cal_main_only(self):
        (self.spool / "accounts.json").unlink()
        self.assertEqual(list(self.new.status({}, {})["accounts"]), ["cal-main"])
        self.assertEqual(self.new.status({"account": "cal-2"})["status"], "UNKNOWN_ACCOUNT")

    def test_mcp_tools_take_account(self):
        src = (HERE / "command_center" / "mcp_tools" / "cal.py").read_text()
        for sig in ("def card_status(account: str = \"\")", "def card_list_archive(account: str = \"\")",
                    "def card_coverage(account: str = \"\")", "account: str = \"\") -> dict:"):
            self.assertIn(sig, src)
        self.assertEqual(src.count("@mcp.tool()"), 6)                  # no new tool names
        app = (HERE / "command_center" / "approvals.py").read_text()
        self.assertIn('p.get("account")', app)

    def test_card_accounts_copy_identical(self):
        self.assertEqual((HERE / "command_center" / "card_accounts.py").read_bytes(),
                         (HERE.parent / "hapoalim-sync" / "host" / "card_accounts.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
