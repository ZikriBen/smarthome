"""Bank Hapoalim: otp-relay guard (login SMS only, transfer/approval SMS rejected), the strict single-code inbox
read, the one-code broker (armed run only, after login start, once per run, TTL, ambiguity), the approval-gated
run request, the sanitized status, and the Unix-socket server. Offline; no bank, no credentials."""
import importlib.util
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
PLUGIN_DIR = HERE.parent / "hermes" / "plugins" / "otp-relay"
os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")
if "command_center.database" not in sys.modules:
    fake_db = types.ModuleType("command_center.database")
    fake_db.connection = sqlite3.connect(":memory:", check_same_thread=False)
    sys.modules["command_center.database"] = fake_db
_db = sys.modules["command_center.database"]
if not hasattr(_db, "approval_lock"):
    _db.approval_lock = threading.Lock()
_db.connection.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, action TEXT NOT NULL, "
                       "payload TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL)")
sys.path.insert(0, str(HERE))
from command_center import hapoalim as hp  # noqa: E402
from command_center import otp_inbox as inbox  # noqa: E402


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "otp_relay_plugin_hp", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["otp_relay_plugin_hp"] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = load_plugin()
BOT = "7000000001"
# UNVERIFIED sample wording (the real bank SMS is not known yet); digits are fake.
LOGIN_SMS = "בנק הפועלים: קוד הכניסה החד פעמי שלך הוא 246810. אין להעביר את הקוד לאף אחד"
TRANSFER_SMSES = (
    "בנק הפועלים: קוד לאישור העברה בסך 5,000 ש\"ח: 135790",
    "בנק הפועלים: הקוד לאישור פעולה באתר הוא 135790",
    "בנק הפועלים: להוספת מוטב חדש הזן קוד 135790",
    "בנק הפועלים: קוד לאישור תשלום 135790 - כניסה לאתר",
    "Bank Hapoalim: code 135790 to approve the transfer. Login",
    "בנק הפועלים: קוד הכניסה 135790 לאישור עסקה בכרטיס",
)


class RelayGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "codes.sqlite"
        self.env = mock.patch.dict(os.environ, {"OTP_FORWARD_BOT_ID": BOT, "OTP_INBOX_DB": str(self.db)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def send(self, text, mid="1"):
        return PLUGIN.consume(BOT, BOT, True, text, message_id=mid)

    def stored(self):
        if not self.db.exists():
            return []
        con = sqlite3.connect(self.db)
        try:
            return [tuple(r) for r in con.execute("SELECT site, code FROM otp_codes")]
        finally:
            con.close()

    def test_login_sms_is_stored_for_hapoalim(self):
        self.assertTrue(self.send(f"#OTP hapoalim {LOGIN_SMS}"))
        self.assertEqual(self.stored(), [("hapoalim", "246810")])

    def test_do_not_share_warning_is_not_a_transfer(self):
        # "אין להעביר את הקוד" (do not pass the code on) is the usual login-SMS warning, not a transfer
        self.assertNotRegex(LOGIN_SMS, PLUGIN.HAPOALIM_REJECT_DEFAULT)

    def test_transfer_payment_beneficiary_action_sms_rejected(self):
        for i, sms in enumerate(TRANSFER_SMSES):
            with self.subTest(sms=sms[:30]):
                self.assertTrue(self.send(f"#OTP hapoalim {sms}", mid=str(i + 10)))  # consumed (never to the LLM)
        self.assertEqual(self.stored(), [])

    def test_rejection_log_has_no_digits_or_text(self):
        with self.assertLogs(PLUGIN.logger, "INFO") as logs:
            self.send(f"#OTP hapoalim {TRANSFER_SMSES[0]}")
        joined = "\n".join(logs.output)
        self.assertNotRegex(joined, r"\d{4,}")
        self.assertNotIn("העברה", joined)

    def test_other_bank_wording_rejected(self):
        self.send("#OTP hapoalim קוד האימות שלך הוא 246810")          # no bank name / no login word
        self.send("#OTP hapoalim בנק הפועלים: עדכון יתרה 246810")      # bank, but not a login SMS
        self.assertEqual(self.stored(), [])

    def test_two_codes_rejected(self):
        self.send("#OTP hapoalim בנק הפועלים קוד כניסה 246810 או 135790")
        self.assertEqual(self.stored(), [])

    def test_bank_sms_under_super_hist_tag_rejected(self):
        self.send(f"#OTP super_hist {LOGIN_SMS}")
        self.assertEqual(self.stored(), [])

    def test_pattern_is_configurable_but_bad_config_fails_closed(self):
        with mock.patch.dict(os.environ, {"OTP_HAPOALIM_REQUIRE_RE": "(unclosed"}):
            self.send(f"#OTP hapoalim {LOGIN_SMS}")
        self.assertEqual(self.stored(), [])
        with mock.patch.dict(os.environ, {"OTP_HAPOALIM_REQUIRE_RE": r"(?=.*קוד הכניסה)"}):
            self.send(f"#OTP hapoalim {LOGIN_SMS}", mid="2")
        self.assertEqual(self.stored(), [("hapoalim", "246810")])

    def test_custom_require_cannot_bypass_reject_list(self):
        with mock.patch.dict(os.environ, {"OTP_HAPOALIM_REQUIRE_RE": r"(?=.*)"}):
            self.send(f"#OTP hapoalim {TRANSFER_SMSES[1]}")
        self.assertEqual(self.stored(), [])

    def test_code_length_configurable(self):
        with mock.patch.dict(os.environ, {"OTP_HAPOALIM_CODE_DIGITS": "8"}):
            self.send("#OTP hapoalim בנק הפועלים: קוד כניסה 12345678")
        self.assertEqual(self.stored(), [("hapoalim", "12345678")])


class Spool(unittest.TestCase):
    """A temp spool + inbox, a fake clock, and a fake approvals module."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (hp.SPOOL_DIR, hp.INBOX_DB, hp.BROKER_DIR, hp.BROKER_SOCK, hp.approvals)
        hp.SPOOL_DIR, hp.INBOX_DB = root / "spool", root / "otp" / "codes.sqlite"
        hp.BROKER_DIR = root / "broker"
        hp.BROKER_SOCK = hp.BROKER_DIR / "broker.sock"
        hp.SPOOL_DIR.mkdir()
        (root / "otp").mkdir()
        self.now = [1_800_000_000.0]
        self.created = []
        hp.approvals = types.SimpleNamespace(create=lambda a, p: (self.created.append((a, p)), "appr1")[1])

    def tearDown(self):
        hp.SPOOL_DIR, hp.INBOX_DB, hp.BROKER_DIR, hp.BROKER_SOCK, hp.approvals = self.saved
        hp.stop_broker()
        self.tmp.cleanup()

    def clock(self):
        return self.now[0]

    def sleep(self, s):
        self.now[0] += s

    def arm(self, run_id="0123456789abcdef", approved_ago=10, name="running.json"):
        (hp.SPOOL_DIR / name).write_text(json.dumps(
            {"run_id": run_id, "month": "2026-09", "approved_at": self.now[0] - approved_ago}))
        return run_id

    def put(self, code, ago=0, site="hapoalim"):
        inbox.put(hp.INBOX_DB, site, code, now=self.now[0] - ago)

    def take(self, run_id="0123456789abcdef", since=None, wait=0):
        msg = {"op": "take_code", "run_id": run_id, "since": self.now[0] - 5 if since is None else since, "wait": wait}
        return hp.broker_handle(msg, now=self.now[0], sleep=self.sleep, clock=self.clock)


class Broker(Spool):
    def test_one_code_for_the_armed_run_then_deleted(self):
        rid = self.arm()
        self.put("246810")
        self.assertEqual(self.take(rid), {"ok": True, "code": "246810"})
        self.assertEqual(inbox.pending(hp.INBOX_DB, now=self.now[0]), [])

    def test_second_take_in_the_same_run_refused(self):
        rid = self.arm()
        self.put("246810")
        self.take(rid)
        self.put("135790")
        self.assertEqual(self.take(rid), {"ok": False, "error": "ALREADY_USED"})

    def test_not_armed_or_wrong_run(self):
        self.put("246810")
        self.assertEqual(self.take(), {"ok": False, "error": "NOT_ARMED"})
        self.arm("fedcba9876543210")
        self.assertEqual(self.take("0123456789abcdef"), {"ok": False, "error": "NOT_ARMED"})
        self.assertEqual(len(inbox.pending(hp.INBOX_DB, now=self.now[0])), 1)  # nothing consumed

    def test_expired_approval_window(self):
        rid = self.arm(approved_ago=hp.RUN_WINDOW + 1)
        self.put("246810")
        self.assertEqual(self.take(rid), {"ok": False, "error": "NOT_ARMED"})

    def test_code_from_before_login_start_never_used(self):
        rid = self.arm(approved_ago=30)
        self.put("246810", ago=20)                              # after approval, before login start
        self.assertEqual(self.take(rid, since=self.now[0] - 5), {"ok": False, "error": "OTP_TIMEOUT"})

    def test_since_is_clamped_to_the_approval(self):
        rid = self.arm(approved_ago=30)
        self.put("246810", ago=60)                              # older than the approval itself
        self.assertEqual(self.take(rid, since=0), {"ok": False, "error": "OTP_TIMEOUT"})

    def test_waits_for_a_late_code(self):
        rid = self.arm()
        original = self.sleep

        def sleep_then_arrive(s):
            original(s)
            if not inbox.pending(hp.INBOX_DB, now=self.now[0]):
                self.put("246810")
        self.sleep = sleep_then_arrive
        self.assertEqual(self.take(rid, wait=30), {"ok": True, "code": "246810"})

    def test_timeout_bounded(self):
        rid = self.arm()
        start = self.now[0]
        self.assertEqual(self.take(rid, wait=10_000), {"ok": False, "error": "OTP_TIMEOUT"})
        self.assertLessEqual(self.now[0] - start, hp.MAX_WAIT + hp.POLL)

    def test_two_distinct_codes_ambiguous_and_both_deleted(self):
        rid = self.arm()
        self.put("246810")
        inbox.put(hp.INBOX_DB, "hapoalim", "135790", ref="tg:x:2", now=self.now[0])
        self.assertEqual(self.take(rid), {"ok": False, "error": "OTP_AMBIGUOUS"})
        self.assertEqual(inbox.pending(hp.INBOX_DB, now=self.now[0]), [])

    def test_short_ttl(self):
        rid = self.arm(approved_ago=600)
        self.put("246810", ago=inbox.SITE_TTL["hapoalim"] + 1)
        self.assertEqual(self.take(rid, since=0), {"ok": False, "error": "OTP_TIMEOUT"})

    def test_only_the_hapoalim_code_is_served(self):
        rid = self.arm()
        self.put("111222", site="super_hist")
        self.assertEqual(self.take(rid), {"ok": False, "error": "OTP_TIMEOUT"})
        self.assertEqual([s for s, _ in inbox.pending(hp.INBOX_DB, now=self.now[0])], ["super_hist"])

    def test_bad_requests(self):
        self.arm()
        for msg in (None, [], {"op": "dump"}, {"op": "take_code", "run_id": "x"},
                    {"op": "take_code", "run_id": "0123456789abcdef", "since": "nan?"}):
            with self.subTest(msg=msg):
                self.assertFalse(hp.broker_handle(msg, now=self.now[0], sleep=self.sleep, clock=self.clock)["ok"])

    def test_otp_required_marker(self):
        rid = self.arm()
        self.assertEqual(hp.broker_handle({"op": "otp_required", "run_id": rid}, now=self.now[0], clock=self.clock),
                         {"ok": True})
        self.assertTrue((hp.SPOOL_DIR / f"otp-requested-{rid}").exists())

    def test_unix_socket_server_round_trip_and_mode(self):
        hp.BROKER_DIR.mkdir()
        self.now[0] = __import__("time").time()                # the real server uses the real clock
        rid = self.arm(approved_ago=1)
        if True:
            hp.start_broker()
            self.assertEqual(os.stat(hp.BROKER_SOCK).st_mode & 0o777, 0o660)
            self.put("246810")
            client = socket.socket(socket.AF_UNIX)
            client.connect(str(hp.BROKER_SOCK))
            client.sendall((json.dumps({"op": "take_code", "run_id": rid, "since": self.now[0] - 5, "wait": 0})
                            + "\n").encode())
            reply = json.loads(client.makefile().readline())
            client.close()
        self.assertEqual(reply, {"ok": True, "code": "246810"})

    def test_broker_inert_without_its_dir(self):
        self.assertIsNone(hp.start_broker())


class RunRequests(Spool):
    def test_propose_needs_approval_and_queues_nothing(self):
        out = hp.propose_run({"month": "2026-09"})
        self.assertEqual(out["status"], "pending_approval")
        self.assertEqual(self.created, [("hapoalim_run_report", {"month": "2026-09"})])
        self.assertFalse((hp.SPOOL_DIR / "request.json").exists())

    def test_bad_month(self):
        self.assertEqual(hp.propose_run({"month": "2026-13"})["status"], "bad_request")
        with self.assertRaises(ValueError):
            hp.execute_run({"month": "../../x"})

    def test_execute_queues_one_request(self):
        out = hp.execute_run({"month": "2026-09"})
        req = json.loads((hp.SPOOL_DIR / "request.json").read_text())
        self.assertEqual(req["run_id"], out["run_id"])
        self.assertRegex(req["run_id"], r"^[0-9a-f]{16}$")
        self.assertEqual(set(req), {"run_id", "month", "approved_at"})
        with self.assertRaises(ValueError):            # a second approval while one is queued
            hp.execute_run({"month": "2026-09"})

    def test_lockout_and_daily_limit_refuse_early(self):
        (hp.SPOOL_DIR / "state.json").write_text(json.dumps({"lockout": {"reason": "OTP_REJECTED", "at": "x"}}))
        self.assertEqual(hp.propose_run({})["status"], "locked_out")
        with self.assertRaises(ValueError):
            hp.execute_run({"month": "2026-09"})
        (hp.SPOOL_DIR / "state.json").write_text(json.dumps({"lastRunDay": hp._today()}))
        self.assertEqual(hp.propose_run({})["status"], "daily_limit")

    def test_default_month_is_last_month(self):
        self.assertRegex(hp.propose_run({})["month"], r"^\d{4}-\d{2}$")

    def test_status_is_sanitized(self):
        (hp.SPOOL_DIR / "status.json").write_text(json.dumps({
            "status": "failed", "errorType": "OTP_TIMEOUT", "error": "free text 12-600-987654",
            "accounts": ["****7654"], "balance": 1234.5, "transactionCount": 3}))
        out = hp.status()
        self.assertEqual(out["last_run"], {"status": "failed", "errorType": "OTP_TIMEOUT", "transactionCount": 3})
        self.assertNotIn("987654", json.dumps(out, ensure_ascii=False))
        self.assertIn("error_text", out)
        self.assertTrue(out["can_run_today"])

    def test_approval_summary_text(self):
        from tests.test_approvals import load_approvals   # a private copy (other tests stub the module)
        text = load_approvals()[0].summarize("hapoalim_run_report", {"month": "2026-09"})
        self.assertIn("2026-09", text)


class InboxUnique(unittest.TestCase):
    def test_take_unique_semantics(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "codes.sqlite"
            self.assertEqual(inbox.take_unique(db, "hapoalim"), (None, "none"))
            self.assertFalse(db.exists())                       # the reader never creates the file
            inbox.put(db, "hapoalim", "246810", ref="a", now=1000)
            inbox.put(db, "hapoalim", "246810", ref="b", now=1001)  # same code twice = one code
            self.assertEqual(inbox.take_unique(db, "hapoalim", since=999, now=1002), ("246810", None))
            self.assertEqual(inbox.take_unique(db, "hapoalim", since=999, now=1003), (None, "none"))


if __name__ == "__main__":
    unittest.main()
