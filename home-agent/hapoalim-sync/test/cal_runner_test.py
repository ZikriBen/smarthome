#!/usr/bin/env python3
"""cal-runner.py tests (no docker, no Cal, synthetic). Uses CAL_RUNNER_TEST_EXEC + test/cal_fake_container.py."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
HOST = HERE / "host"
sys.path.insert(0, str(HOST))
spec = importlib.util.spec_from_file_location("cal_runner", HOST / "cal-runner.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
import otp_inbox  # noqa: E402
import cal_archive  # noqa: E402

FAKE = HERE / "test" / "cal_fake_container.py"


class CalRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("out", "state", "spool", "otp", "creds", "archive"):
            (self.root / name).mkdir()
        (self.root / "creds" / "cal.username").write_text("caluser")
        (self.root / "creds" / "cal.password").write_text("calpass\n")
        self.inbox = self.root / "otp" / "codes.sqlite"
        self.logins = self.root / "logins.txt"
        self.env = {
            "CAL_SYNC_IMAGE": "unused", "CAL_OUT_DIR": str(self.root / "out"),
            "CAL_ARCHIVE_DIR": str(self.root / "archive"), "CAL_STATE_DIR": str(self.root / "state"),
            "CAL_SPOOL_DIR": str(self.root / "spool"), "CAL_INBOX_DB": str(self.inbox),
            "CAL_OTP_TIMEOUT": "2", "CAL_RUN_DEADLINE": "60", "CREDENTIALS_DIRECTORY": str(self.root / "creds"),
            "CAL_OUT_UID": str(os.getuid()), "CAL_OUT_GID": str(os.getgid()),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, behaviour="ok", **over):
        env = dict(self.env, CAL_RUNNER_TEST_EXEC=json.dumps([sys.executable, "-u", str(FAKE), behaviour, str(self.logins)]))
        env.update(over)
        return cr.CalRunner(cr.CalConfig(env))

    def queue(self, actions=None):
        req = {"run_id": os.urandom(8).hex(), "requested_at": time.time(),
               "actions": actions or [{"name": "card_transactions", "from_month": "2026-02", "to_month": "2026-04"}]}
        (self.root / "spool" / "request.json").write_text(json.dumps(req))
        return req

    def logins_count(self):
        return len(self.logins.read_text().splitlines()) if self.logins.exists() else 0

    def test_config_defaults(self):
        cfg = cr.CalConfig({})
        self.assertEqual(cfg.max_runs, 2)
        self.assertEqual(str(cfg.archive_dir), "/home/ben/smarthome/data/cal/archive")
        self.assertEqual(str(cfg.spool_dir), "/var/lib/cal-spool")
        self.assertEqual(cr.CalConfig({"CAL_MAX_RUNS_PER_DAY": "10"}).max_runs, 10)
        self.assertEqual(cr.CalConfig({"CAL_MAX_RUNS_PER_DAY": "11"}).max_runs, 2)
        self.assertEqual(cr.CalConfig({"HAPOALIM_SYNC_IMAGE": "sha256:x"}).image, "sha256:x")
        self.assertEqual(cr.CalConfig({"HAPOALIM_SYNC_IMAGE": "a", "CAL_SYNC_IMAGE": "b"}).image, "b")

    def test_container_argv_hardened(self):
        argv = cr.CalRunner(cr.CalConfig({"CAL_SYNC_IMAGE": "sha256:abc"})).container_argv("0123456789abcdef")
        for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--log-driver"):
            self.assertIn(flag, argv)
        i = argv.index("sha256:abc")
        self.assertEqual(argv[i - 2:i], ["--entrypoint", "dumb-init"])
        self.assertEqual(argv[i + 1:], ["--", "node", "/app/cal-auto.mjs"])
        self.assertNotIn("-v", argv)
        self.assertFalse(any("password" in a.lower() or "caluser" in a for a in argv))

    def test_api_transport_switch(self):
        argv = cr.CalRunner(cr.CalConfig({"CAL_SYNC_IMAGE": "sha256:abc"})).container_argv("0123456789abcdef")
        self.assertIn("CAL_API_TRANSPORT=page", argv)
        argv = cr.CalRunner(cr.CalConfig({"CAL_SYNC_IMAGE": "sha256:abc", "CAL_API_TRANSPORT": "node"})).container_argv("0123456789abcdef")
        self.assertIn("CAL_API_TRANSPORT=node", argv)
        argv = cr.CalRunner(cr.CalConfig({"CAL_SYNC_IMAGE": "sha256:abc", "CAL_API_TRANSPORT": "node;x"})).container_argv("0123456789abcdef")
        self.assertIn("CAL_API_TRANSPORT=page", argv)

    def test_happy_path_archive_and_status(self):
        self.queue()
        status, code = self.runner().run()
        self.assertEqual(code, 0, status)
        self.assertEqual(status["status"], "DONE")
        self.assertTrue(status["csv"].startswith("cal-"))
        self.assertEqual(oct((self.root / "out" / status["csv"]).stat().st_mode & 0o777), "0o600")
        self.assertNotIn("secretField", status)
        self.assertNotIn("detail", json.dumps(status.get("actions")))
        arch = self.root / "archive"
        self.assertEqual(oct(arch.stat().st_mode & 0o777), "0o700")
        for c in ("1111", "2222"):
            for m in ("2026-02", "2026-03", "2026-04"):
                p = arch / f"cal-card-{c}-{m}.csv"
                self.assertTrue(p.is_file(), p)
                self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        self.assertTrue((arch / "cal-card-1111-pending.csv").is_file())
        self.assertEqual(oct((arch / "_coverage.json").stat().st_mode & 0o777), "0o600")
        runs = json.loads((self.root / "spool" / "runs-today.json").read_text())
        self.assertEqual((runs["runs"], runs["max_runs"]), (1, 2))
        self.assertEqual(self.logins_count(), 1)
        # Second identical request: closed + final months -> COVERED without a login, not counted.
        self.queue()
        status, code = self.runner().run()
        self.assertEqual(status["status"], "COVERED")
        self.assertEqual(self.logins_count(), 1)

    def test_open_months_never_covered(self):
        cur = cal_archive.month_il()
        acts = [{"name": "card_transactions", "from_month": cur, "to_month": cur}]
        self.queue(acts)
        self.assertEqual(self.runner().run()[0]["status"], "DONE")
        self.queue(acts)
        self.assertEqual(self.runner().run()[0]["status"], "DONE")
        self.assertEqual(self.logins_count(), 2)

    def test_daily_limit_default_two(self):
        cur = cal_archive.month_il()
        acts = [{"name": "card_transactions", "from_month": cur, "to_month": cur}]
        for _ in range(2):
            self.queue(acts)
            self.assertEqual(self.runner().run()[0]["status"], "DONE")
        self.queue(acts)
        status, code = self.runner().run()
        self.assertEqual(status["status"], "DAILY_LIMIT")
        self.assertEqual((status["runsToday"], status["maxRuns"]), (2, 2))
        self.assertEqual(self.logins_count(), 2)
        # drop-in override (CAL_MAX_RUNS_PER_DAY=3) -> runs again, published max_runs follows
        self.queue(acts)
        status, _ = self.runner(CAL_MAX_RUNS_PER_DAY="3").run()
        self.assertEqual(status["status"], "DONE")
        self.assertEqual(json.loads((self.root / "spool" / "runs-today.json").read_text())["max_runs"], 3)

    def test_auth_failure_stops_for_today(self):
        self.queue()
        status, code = self.runner("invalid").run()
        self.assertEqual(status["status"], "INVALID_PASSWORD")
        self.assertTrue(status["stopForToday"])
        self.assertTrue((self.root / "state" / f"stop-{cr.base.today()}").is_file())
        self.queue()
        status, _ = self.runner().run()
        self.assertEqual(status["status"], "STOPPED_TODAY")
        self.assertEqual(self.logins_count(), 1)

    def test_otp_uses_site_cal_only(self):
        self.queue()
        otp_inbox.put(self.inbox, "hapoalim", "111111")   # another site's code must never be used
        threading.Thread(target=lambda: (time.sleep(0.6), otp_inbox.put(self.inbox, "cal", "246810")), daemon=True).start()
        status, _ = self.runner("otp").run()
        self.assertEqual(status["status"], "DONE", status)
        self.assertEqual(status["otpWait"], "delivered")
        self.assertIsNone(otp_inbox.take(self.inbox, "cal"))
        self.assertEqual(otp_inbox.take(self.inbox, "hapoalim"), "111111")

    def test_otp_timeout_stop(self):
        self.queue()
        status, _ = self.runner("otp-timeout", CAL_OTP_TIMEOUT="1").run()
        self.assertEqual(status["status"], "OTP_TIMEOUT")
        self.assertTrue(status["stopForToday"])

    def test_blocked_partial_ingests_and_stops(self):
        self.queue([{"name": "card_transactions", "from_month": "2026-02", "to_month": "2026-04"}])
        status, _ = self.runner("blocked").run()
        self.assertEqual(status["status"], "DONE")
        self.assertTrue(status["stopForToday"])
        cov = cal_archive.load_coverage(self.root / "archive")
        self.assertIn("2026-04", cov["cards"]["1111"]["months"])
        self.assertNotIn("2026-03", cov["cards"]["1111"]["months"])
        self.assertTrue(any("stopped" in w for w in status["archiveWarnings"]))

    def test_not_configured(self):
        (self.root / "creds" / "cal.password").unlink()
        self.queue()
        self.assertEqual(self.runner().run()[0]["status"], "NOT_CONFIGURED")
        self.assertEqual(self.logins_count(), 0)

    def test_retention_only_cal_csvs(self):
        old = time.time() - 30 * 86400
        for n in ("cal-20260101-000000-card_transactions.csv", "hapoalim-20260101-000000-checking_transactions.csv"):
            p = self.root / "out" / n
            p.write_text("x")
            os.utime(p, (old, old))
        self.queue()
        self.runner().run()
        self.assertFalse((self.root / "out" / "cal-20260101-000000-card_transactions.csv").exists())
        self.assertTrue((self.root / "out" / "hapoalim-20260101-000000-checking_transactions.csv").exists())


if __name__ == "__main__":
    unittest.main()
