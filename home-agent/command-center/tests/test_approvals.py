"""approvals.decide(): first decision wins, idempotent replays, expiry, audit-only rows."""
import os
import sqlite3
import sys
import threading
import types
import unittest
from unittest import mock

os.environ.setdefault("COMMAND_CENTER_TOKEN", "test")


def load_approvals():
    """A fresh command_center.approvals bound to a private in-memory database.

    The module is executed from source into a private module object, so neither the shared
    ``command_center.database`` stub used by other test modules nor ``sys.modules`` is touched.
    """
    import importlib.util
    from pathlib import Path
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE approvals (id TEXT PRIMARY KEY, action TEXT NOT NULL, "
                 "payload TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL)")
    db = types.ModuleType("approvals_test_db")
    db.connection, db.approval_lock = conn, threading.Lock()
    source = (Path(__file__).resolve().parents[1] / "command_center" / "approvals.py").read_text()
    source = source.replace("from .database import approval_lock, connection",
                            "approval_lock, connection = _db.approval_lock, _db.connection")
    module = types.ModuleType("approvals_under_test")
    module._db = db
    exec(compile(source, "approvals.py", "exec"), module.__dict__)
    return module, conn


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.ap, self.db = load_approvals()
        self.calls = []
        self.ap.register_action("docker_restart", lambda p: self.calls.append(p) or {"restarted": p["container"]})
        self.ap.register_action("calendar_create", lambda p: {"created": {"summary": p["summary"]}})

    def status(self, ident):
        return self.db.execute("SELECT status FROM approvals WHERE id=?", (ident,)).fetchone()[0]

    def test_approve_executes_once_and_reports_executed(self):
        ident = self.ap.create("docker_restart", {"container": "jellyfin"})
        first = self.ap.decide(ident, "approve", via="chat", actor="Ben")
        self.assertEqual(first["status"], "executed")
        self.assertFalse(first["already_decided"])
        self.assertEqual(first["decided"]["via"], "chat")
        self.assertEqual(first["result"], {"restarted": "jellyfin"})
        self.assertEqual(self.calls, [{"container": "jellyfin"}])

    def test_second_decision_is_idempotent_and_names_the_first(self):
        ident = self.ap.create("docker_restart", {"container": "jellyfin"})
        self.ap.decide(ident, "approve", via="chat", actor="Ben")
        again = self.ap.decide(ident, "approve", via="button", actor="Ben")
        self.assertTrue(again["already_decided"])
        self.assertEqual(again["status"], "executed")
        self.assertEqual(again["decided"]["via"], "chat")
        deny_late = self.ap.decide(ident, "deny", via="button", actor="Alice")
        self.assertTrue(deny_late["already_decided"])
        self.assertEqual(deny_late["status"], "executed")
        self.assertEqual(len(self.calls), 1)

    def test_deny_then_approve_never_executes(self):
        ident = self.ap.create("docker_restart", {"container": "jellyfin"})
        self.assertEqual(self.ap.decide(ident, "deny", via="chat", actor="Ben")["status"], "denied")
        late = self.ap.decide(ident, "approve", via="button", actor="Ben")
        self.assertEqual(late["status"], "denied")
        self.assertTrue(late["already_decided"])
        self.assertEqual(self.calls, [])

    def test_concurrent_decisions_execute_once(self):
        ident = self.ap.create("docker_restart", {"container": "jellyfin"})
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.ap.decide(ident, "approve")))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(1 for r in results if not r["already_decided"]), 1)

    def test_expired_is_reported_and_never_executes(self):
        ident = self.ap.create("docker_restart", {"container": "jellyfin"})
        with mock.patch.object(self.ap.time, "time", return_value=10**10):
            out = self.ap.decide(ident, "approve")
        self.assertEqual(out["status"], "expired")
        self.assertFalse(out["already_decided"])
        self.assertEqual(self.status(ident), "expired")
        self.assertEqual(self.calls, [])

    def test_expire_stale_sweeps_old_pending(self):
        old = self.ap.create("docker_restart", {"container": "a"})
        self.db.execute("UPDATE approvals SET created=0 WHERE id=?", (old,))
        fresh = self.ap.create("docker_restart", {"container": "b"})
        self.ap.expire_stale()
        self.assertEqual(self.status(old), "expired")
        self.assertEqual(self.status(fresh), "pending")

    def test_handler_failure_is_failed_with_error(self):
        self.ap.register_action("gmail_send", mock.Mock(side_effect=RuntimeError("smtp down")))
        ident = self.ap.create("gmail_send", {"to": "a@b.c", "subject": "s", "body": "b"})
        out = self.ap.decide(ident, "approve")
        self.assertEqual(out["status"], "failed")
        self.assertIn("smtp down", out["error"])
        self.assertEqual(self.status(ident), "failed")
        self.assertEqual(self.ap.get(ident)["decided"]["error"], "smtp down")

    def test_unknown_and_audit_rows_are_not_decidable(self):
        self.assertEqual(self.ap.decide("f" * 18, "approve")["status"], "unknown")
        audit = self.ap.create("searchgram_download", {"x": 1}, status="pending")
        self.assertEqual(self.ap.decide(audit, "approve")["status"], "unknown")
        self.assertEqual(self.ap.get(audit)["status"], "unknown")

    def test_get_has_summary_and_expiry_but_no_payload(self):
        ident = self.ap.create("calendar_create", {"summary": "בריכה", "start": "2026-10-23T08:00:00+03:00",
                                                   "end": "2026-10-23T10:00:00+03:00"})
        view = self.ap.get(ident)
        self.assertEqual(view["status"], "pending")
        self.assertIn("בריכה", view["summary"])
        self.assertIn("23.10 08:00", view["summary"])
        self.assertEqual(view["expires_at"], view["created"] + self.ap.TTL_SECONDS)
        self.assertNotIn("payload", view)

    def test_compat_wrappers_keep_old_contract(self):
        ident = self.ap.create("docker_restart", {"container": "x"})
        self.assertEqual(self.ap.approve(ident)["status"], "executed")
        with self.assertRaises(ValueError):
            self.ap.approve(ident)
        other = self.ap.create("docker_restart", {"container": "y"})
        self.assertEqual(self.ap.deny(other)["status"], "denied")
        with self.assertRaises(ValueError):
            self.ap.deny(other)

    def test_summaries_for_every_action(self):
        s = self.ap.summarize
        self.assertIn("jellyfin", s("docker_restart", {"container": "jellyfin"}))
        self.assertIn("a@b.c", s("gmail_send", {"to": "a@b.c", "subject": "hi"}))
        self.assertIn("סדרה", s("calendar_delete", {"scope": "series", "event": {"summary": "x", "start": "2026-10-23"}}))
        self.assertIn("→", s("calendar_update", {"before": {"summary": "a"}, "after": {"summary": "b"}}))
        self.assertEqual(s("weird_action", None), "weird action")


if __name__ == "__main__":
    unittest.main()


class BackgroundActionTests(unittest.TestCase):
    """register_action(background=True): approve → running; the worker's finish_background → executed/failed
    with the result text Hermes pushes to Telegram (via the existing GET /v1/approvals/<id>)."""

    def setUp(self):
        self.ap, self.db = load_approvals()
        self.started = []
        self.ap.register_action("bg", lambda payload, ident: self.started.append((payload, ident)) or
                                {"status": "started"}, background=True)

    def tearDown(self):
        self.db.close()

    def test_running_then_executed_with_result_text(self):
        ident = self.ap.create("bg", {"x": 1})
        out = self.ap.decide(ident, "approve", via="button", actor="ben")
        self.assertEqual(out["status"], "running")
        self.assertEqual(self.started, [({"x": 1}, ident)])
        self.assertEqual(self.ap.get(ident)["status"], "running")
        self.assertTrue(self.ap.finish_background(ident, True, "✅ נכתב: חלב ×2"))
        view = self.ap.get(ident)
        self.assertEqual(view["status"], "executed")
        self.assertEqual(view["result_text"], "✅ נכתב: חלב ×2")
        self.assertNotIn("payload", view)
        self.assertFalse(self.ap.finish_background(ident, False, "again"))     # once only

    def test_failed_result_and_second_tap(self):
        ident = self.ap.create("bg", {})
        self.ap.decide(ident, "approve")
        again = self.ap.decide(ident, "approve")
        self.assertTrue(again["already_decided"])
        self.assertEqual(len(self.started), 1)
        self.ap.finish_background(ident, False, "⚠️ חלקי\nלא עודכנו: חלב")
        view = self.ap.get(ident)
        self.assertEqual(view["status"], "failed")
        self.assertEqual(view["decided"]["error"], "⚠️ חלקי")
        self.assertIn("לא עודכנו", view["result_text"])

    def test_worker_faster_than_decide(self):
        def fast(payload, ident):
            self.ap.finish_background(ident, True, "done fast")
            return {"status": "started"}
        self.ap.register_action("fast", fast, background=True)
        ident = self.ap.create("fast", {})
        self.assertEqual(self.ap.decide(ident, "approve")["status"], "executed")
        self.assertEqual(self.ap.get(ident)["result_text"], "done fast")

    def test_restart_marks_running_failed(self):
        ident = self.ap.create("bg", {})
        self.ap.decide(ident, "approve")
        source_db = types.ModuleType("approvals_test_db2")
        source_db.connection, source_db.approval_lock = self.db, threading.Lock()
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "command_center" / "approvals.py").read_text()
        source = source.replace("from .database import approval_lock, connection",
                                "approval_lock, connection = _db.approval_lock, _db.connection")
        restarted = types.ModuleType("approvals_restarted")
        restarted._db = source_db
        exec(compile(source, "approvals.py", "exec"), restarted.__dict__)       # a new process on the same DB
        view = restarted.get(ident)
        self.assertEqual(view["status"], "failed")
        self.assertEqual(view["result_text"], restarted.RESTART_TEXT)
