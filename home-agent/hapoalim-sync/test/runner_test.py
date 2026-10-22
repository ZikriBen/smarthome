#!/usr/bin/env python3
"""Host-runner tests (no docker, no bank, no real Telegram). Uses HAPOALIM_RUNNER_TEST_EXEC."""
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

spec = importlib.util.spec_from_file_location("hapoalim_runner", HOST / "hapoalim-runner.py")
hr = importlib.util.module_from_spec(spec)
sys.modules["hapoalim_runner"] = hr
spec.loader.exec_module(hr)

import otp_inbox  # noqa: E402

Config, Runner, cleanup_old_csv, today = hr.Config, hr.Runner, hr.cleanup_old_csv, hr.today


def write_fake_container(path, behaviour="ok", delay=0.0, login_counter=None):
    nl = chr(10)
    counter = ""
    if login_counter is not None:
        counter = "open(%r, 'a').write('1' + chr(10))" % (str(login_counter),) + nl
    path.write_text(
        "#!/usr/bin/env python3" + nl +
        "import json, sys, time" + nl +
        counter +
        "line = sys.stdin.readline()" + nl +
        "creds = json.loads(line)" + nl +
        "assert 'userCode' in creds and 'password' in creds" + nl +
        "actions = creds.get('actions') or [{'name':'checking_transactions'}]" + nl +
        f"time.sleep({delay})" + nl +
        "sys.stdout.write(json.dumps({'event':'otp_needed'}) + chr(10)); sys.stdout.flush()" + nl +
        "reply = json.loads(sys.stdin.readline())" + nl +
        "code = reply.get('otp')" + nl +
        f"if {behaviour!r} == 'hang-otp':" + nl +
        "    time.sleep(1000)" + nl +
        "if not code:" + nl +
        "    report = {'status': reply.get('error') or 'OTP_TIMEOUT', 'stage':'otp-requested',"
        " 'stopForToday': True, 'otpRequested': True, 'otpDelivered': False}" + nl +
        "    sys.stdout.write(json.dumps({'event':'result','report':report,'files':[]}) + chr(10)); sys.exit(1)" + nl +
        "csv = ('date,description,amount,account_balance_now,reference,account,processed_date,memo,currency,status,type'"
        " + chr(10) + '2026-10-01,x,-1,,R1,12345678,2026-10-01,,ILS,completed,normal' + chr(10))" + nl +
        "arch = json.dumps({'from': actions[0].get('from') or '2026-09-01', 'to': actions[0].get('to') or '2026-10-22',"
        "'accounts':['12345678'],'rows':[{'account':'12345678','date':'2026-10-01','processed_date':'2026-10-01',"
        "'amount':-1,'description':'x','status':'completed','identifier':'R1','currency':'ILS'}]})" + nl +
        "report = {'status':'DONE','stage':'done','stopForToday':False,'otpRequested':True,'otpDelivered':True,"
        "'accountCount':1,'transactionCount':1,'firstDate':'2026-10-01','lastDate':'2026-10-01',"
        "'from':actions[0].get('from') or '2026-09-01','to':actions[0].get('to') or '2026-10-22',"
        "'blockedHosts':[],'failedHosts':[],'profileWiped':True,"
        "'actions':[{'name':a.get('name'),'ok':True,'status':'DONE'} for a in actions]}" + nl +
        "sys.stdout.write(json.dumps({'event':'result','report':report,"
        "'files':[{'kind':'csv','nameHint':'checking_transactions','body':csv},"
        "{'kind':'archive_rows','nameHint':'archive','body':arch}]}) + chr(10))" + nl
    )
    path.chmod(0o755)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("out", "state", "spool", "otp", "creds", "archive"):
            (self.root / name).mkdir()
        (self.root / "creds" / "hapoalim.user_code").write_text("user1")
        (self.root / "creds" / "hapoalim.password").write_text("pass1")
        self.inbox = self.root / "otp" / "codes.sqlite"
        self.fake = self.root / "fake-container.py"
        write_fake_container(self.fake)
        self.env = {
            "HAPOALIM_SYNC_IMAGE": "unused",
            "HAPOALIM_OUT_DIR": str(self.root / "out"),
            "HAPOALIM_ARCHIVE_DIR": str(self.root / "archive"),
            "HAPOALIM_STATE_DIR": str(self.root / "state"),
            "HAPOALIM_SPOOL_DIR": str(self.root / "spool"),
            "HAPOALIM_INBOX_DB": str(self.inbox),
            "HAPOALIM_OTP_TIMEOUT": "2",
            "HAPOALIM_RUN_DEADLINE": "30",
            "HAPOALIM_RETENTION_DAYS": "7",
            "CREDENTIALS_DIRECTORY": str(self.root / "creds"),
            "HAPOALIM_RUNNER_TEST_EXEC": json.dumps([sys.executable, "-u", str(self.fake)]),
            "HAPOALIM_OUT_UID": str(os.getuid()),
            "HAPOALIM_OUT_GID": str(os.getgid()),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **over):
        return Config(dict(self.env, **over))

    def queue(self, actions=None, requested_at=None):
        req = {"run_id": os.urandom(8).hex(),
               "requested_at": time.time() if requested_at is None else requested_at,
               "actions": actions or [{"name": "checking_transactions", "from": "2026-09-01", "to": "2026-10-22"}]}
        (self.root / "spool" / "request.json").write_text(json.dumps(req))
        return req

    def put_code(self, site, code, when=None):
        otp_inbox.put(self.inbox, site, code, now=time.time() if when is None else when)

    def test_happy_path_delayed_otp(self):
        self.queue()
        threading.Thread(target=lambda: (time.sleep(0.8), self.put_code("hapoalim", "135790")), daemon=True).start()
        status, code = Runner(self.cfg()).run()
        self.assertEqual(code, 0)
        self.assertEqual(status["status"], "DONE")
        self.assertEqual(status["otpWait"], "delivered")
        self.assertTrue((self.root / "out" / status["csv"]).is_file())
        self.assertEqual(oct((self.root / "out" / status["csv"]).stat().st_mode & 0o777), "0o600")
        self.assertIsNone(otp_inbox.take(self.inbox, "hapoalim"))

    def test_otp_timeout_writes_stop(self):
        self.queue()
        status, code = Runner(self.cfg(HAPOALIM_OTP_TIMEOUT="1")).run()
        self.assertEqual(status["status"], "OTP_TIMEOUT")
        self.assertTrue(status["stopForToday"])
        self.assertTrue((self.root / "state" / f"stop-{today()}").is_file())

    def test_stale_code_ignored(self):
        self.put_code("hapoalim", "111111", when=time.time() - 60)
        self.queue(requested_at=time.time())
        threading.Thread(target=lambda: (time.sleep(0.5), self.put_code("hapoalim", "222222")), daemon=True).start()
        status, code = Runner(self.cfg()).run()
        self.assertEqual(status["status"], "DONE")
        self.assertIsNone(otp_inbox.take(self.inbox, "hapoalim"))

    def test_super_hist_code_not_picked_up(self):
        self.queue()
        def flood():
            for _ in range(6):
                self.put_code("super_hist", "654321")
                time.sleep(0.15)
        threading.Thread(target=flood, daemon=True).start()
        status, code = Runner(self.cfg(HAPOALIM_OTP_TIMEOUT="1")).run()
        self.assertEqual(status["status"], "OTP_TIMEOUT")
        self.assertEqual(otp_inbox.take(self.inbox, "super_hist"), "654321")

    def test_stop_for_today_refuses(self):
        (self.root / "state" / f"stop-{today()}").write_text("{}")
        self.queue()
        status, code = Runner(self.cfg()).run()
        self.assertEqual(status["status"], "STOPPED_TODAY")
        self.assertEqual(code, 2)

    def test_concurrent_lock(self):
        write_fake_container(self.fake, delay=0.5)
        self.queue()
        results = []
        def run_first():
            time.sleep(0.2)
            self.put_code("hapoalim", "999888")
            results.append(Runner(self.cfg()).run())
        # Hold the lock in this thread; the other thread should see LOCKED.
        cfg = self.cfg()
        import fcntl
        lock = open(cfg.state_dir / "run.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            self.queue()
            status2, code2 = Runner(cfg).run()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        self.assertEqual(status2["status"], "LOCKED")
        self.assertEqual(code2, 3)

    def test_retention_cleanup(self):
        old = self.root / "out" / "hapoalim-20260101-120000-checking_transactions.csv"
        old.write_text("x\n")
        os.utime(old, (time.time() - 10 * 86400, time.time() - 10 * 86400))
        self.assertEqual(cleanup_old_csv(self.root / "out", 7), 1)
        self.assertFalse(old.exists())


    def test_covered_skips_login(self):
        import archive as arch
        counter = self.root / "logins.txt"
        write_fake_container(self.fake, login_counter=counter)
        arch.merge_sync(self.root / "archive", [], range_from="2026-09-01", range_to="2026-09-30",
                        run_id="seed", accounts=["12345678"])
        self.queue(actions=[{"name": "checking_transactions", "from": "2026-09-01", "to": "2026-09-30"}])
        status, code = Runner(self.cfg(HAPOALIM_REPULL_DAYS="3")).run()
        self.assertEqual(status["status"], "COVERED")
        self.assertEqual(code, 0)
        self.assertFalse(counter.exists(), "no login when COVERED")
        self.assertFalse((self.root / "state" / f"runs-{today()}").exists() and
                         (self.root / "state" / f"runs-{today()}").read_text().strip() not in ("", "0"))

    def test_sync_merges_archive_one_login(self):
        counter = self.root / "logins.txt"
        write_fake_container(self.fake, login_counter=counter)
        self.queue()
        threading.Timer(0.5, lambda: self.put_code("hapoalim", "12345")).start()
        status, code = Runner(self.cfg(HAPOALIM_OTP_TIMEOUT="5")).run()
        self.assertEqual(status["status"], "DONE", status)
        self.assertGreaterEqual(status.get("archiveNew", 0), 1)
        f = self.root / "archive" / "hapoalim-5678-2026-10.csv"
        self.assertTrue(f.is_file())
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        self.assertEqual(counter.read_text().count("1"), 1)
        cov = json.loads((self.root / "archive" / "_coverage.json").read_text())
        self.assertTrue(cov["accounts"]["5678"]["months"]["2026-09"]["complete"])
        # archive_rows never lands in out/
        self.assertEqual([p.name for p in (self.root / "out").iterdir() if "archive" in p.name], [])


if __name__ == "__main__":
    unittest.main()
