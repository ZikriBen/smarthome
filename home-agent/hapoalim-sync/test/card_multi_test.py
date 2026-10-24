#!/usr/bin/env python3
"""Multi-account card sync (host side): registry, routing, per-account budget / stop isolation, global cap, the
shared bank lock, MIGRATION_PENDING, migration round-trip. Synthetic only: fake container, fake creds, no Cal."""
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
HOST = HERE / "host"
sys.path.insert(0, str(HOST))
spec = importlib.util.spec_from_file_location("cal_runner_multi", HOST / "cal-runner.py")
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
mspec = importlib.util.spec_from_file_location("card_migrate", HOST / "card-migrate.py")
mig = importlib.util.module_from_spec(mspec)
mspec.loader.exec_module(mig)
import cal_archive  # noqa: E402
import card_accounts as ca  # noqa: E402

FAKE = HERE / "test" / "cal_fake_container.py"
CUR = cal_archive.month_il()
OPEN = [{"name": "card_transactions", "from_month": CUR, "to_month": CUR}]   # open month: never COVERED


def tree(root):
    """{relative path: sha256} for every regular file below root (the 'tree hash' of a data layout)."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and not p.is_symlink() and p.name not in (".lock", "run.lock") and "migrations" not in p.parts:
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("out", "state", "spool", "otp", "creds", "archive", "hapoalim-state"):
            (self.root / name).mkdir()
        self.creds = self.root / "creds"
        self.registry = self.root / "card-accounts.json"
        self.logins = self.root / "logins.txt"
        self.env = {
            "CAL_SYNC_IMAGE": "unused", "CAL_OUT_DIR": str(self.root / "out"),
            "CAL_ARCHIVE_DIR": str(self.root / "archive"), "CAL_STATE_DIR": str(self.root / "state"),
            "CAL_SPOOL_DIR": str(self.root / "spool"), "CAL_INBOX_DB": str(self.root / "otp" / "codes.sqlite"),
            "CAL_OTP_TIMEOUT": "2", "CAL_RUN_DEADLINE": "60", "CREDENTIALS_DIRECTORY": str(self.creds),
            "CAL_OUT_UID": str(os.getuid()), "CAL_OUT_GID": str(os.getgid()),
            "CARD_ACCOUNTS": str(self.registry), "CARD_BANK_LOCK": str(self.root / "hapoalim-state" / "run.lock"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def two_accounts(self, **cal2):
        reg = {"version": 1, "accounts": [
            {"id": "cal-main", "provider": "cal", "label": "Cal", "enabled": True},
            dict({"id": "cal-2", "provider": "cal", "label": "Cal 2", "enabled": True}, **cal2)]}
        self.registry.write_text(json.dumps(reg))
        (self.creds / "cal.username").write_text("caluser")            # cal-main: legacy names (old cal-setup)
        (self.creds / "cal.password").write_text("calpass\n")
        (self.creds / "card-cal-2.username").write_text("cal2user")
        (self.creds / "card-cal-2.password").write_text("cal2pass\n")

    def runner(self, behaviour="ok", **over):
        env = dict(self.env, CAL_RUNNER_TEST_EXEC=json.dumps([sys.executable, "-u", str(FAKE), behaviour, str(self.logins)]))
        env.update(over)
        return cr.CalRunner(cr.CalConfig(env))

    def queue(self, account=None, actions=None):
        req = {"run_id": os.urandom(8).hex(), "requested_at": time.time(), "actions": actions or OPEN}
        if account:
            req["account"] = account
        (self.root / "spool" / "request.json").write_text(json.dumps(req))
        return req

    def sync(self, account=None, behaviour="ok", actions=None, **over):
        self.queue(account, actions)
        return self.runner(behaviour, **over).run()[0]

    def users(self):
        return [l.split(" ", 1)[1] for l in self.logins.read_text().splitlines()] if self.logins.exists() else []


class RegistryTests(Base):
    def test_missing_registry_is_cal_main_only(self):
        reg = ca.load_registry(self.registry)
        self.assertEqual([a["id"] for a in reg["accounts"]], ["cal-main"])
        self.assertEqual(ca.runnable(reg, "cal-main")[1].name, "cal")

    def test_validation_fails_closed(self):
        for bad in ({"accounts": [{"id": "A", "provider": "cal"}]},
                    {"accounts": [{"id": "x", "provider": "cal"}]},                       # too short
                    {"accounts": [{"id": "a" * 25, "provider": "cal"}]},                  # too long
                    {"accounts": [{"id": "cal-2", "provider": "cal"}, {"id": "cal-2", "provider": "cal"}]},
                    {"accounts": [{"id": "cal-2", "provider": "cal", "max_runs_per_day": 0}]},
                    {"accounts": []}, []):
            self.registry.write_text(json.dumps(bad))
            with self.assertRaises(ca.RegistryError):
                ca.load_registry(self.registry)
        self.registry.write_text("{broken")
        with self.assertRaises(ca.RegistryError) as ctx:
            ca.load_registry(self.registry)
        self.assertEqual(ctx.exception.code, "BAD_REGISTRY")

    def test_providers_only_cal(self):
        for name in ("max", "isracard", "amex", "leumi"):
            with self.assertRaises(ca.RegistryError) as ctx:
                ca.provider(name)
            self.assertEqual(ctx.exception.code, "NOT_SUPPORTED")
            self.assertIn("not supported yet", ctx.exception.text)
        self.assertEqual(ca.PROVIDERS["cal"].login_fields, ("username", "password"))
        reg = ca.validate({"accounts": [{"id": "max-1", "provider": "max"}, {"id": "cal-2", "provider": "cal",
                                                                           "enabled": False}]})
        with self.assertRaises(ca.RegistryError) as ctx:
            ca.runnable(reg, "max-1")
        self.assertEqual(ctx.exception.code, "NOT_SUPPORTED")
        with self.assertRaises(ca.RegistryError) as ctx:
            ca.runnable(reg, "cal-2")
        self.assertEqual(ctx.exception.code, "ACCOUNT_DISABLED")
        with self.assertRaises(ca.RegistryError) as ctx:
            ca.runnable(reg, "cal-9")
        self.assertEqual(ctx.exception.code, "UNKNOWN_ACCOUNT")
        m = ca.mirror(reg)
        self.assertEqual([(a["id"], a["supported"]) for a in m["accounts"]], [("max-1", False), ("cal-2", True)])

    def test_credentials_new_names_win_legacy_only_for_cal_main(self):
        prov = ca.PROVIDERS["cal"]
        (self.creds / "cal.username").write_text("legacyuser")
        (self.creds / "cal.password").write_text("legacypass\n")
        self.assertEqual(ca.read_credentials(self.creds, "cal-main", prov), {"username": "legacyuser", "password": "legacypass"})
        self.assertIsNone(ca.read_credentials(self.creds, "cal-2", prov))            # never borrows cal.*
        (self.creds / "card-cal-main.username").write_text("newuser")
        (self.creds / "card-cal-main.password").write_text("newpass")
        self.assertEqual(ca.read_credentials(self.creds, "cal-main", prov)["username"], "newuser")
        (self.creds / "card-cal-main.password").unlink()                               # half-new: refuse, no mixing
        self.assertIsNone(ca.read_credentials(self.creds, "cal-main", prov))

    def test_dropin_lists_only_existing_files(self):
        self.two_accounts()
        store = self.root / "store"
        store.mkdir()
        for n in ("cal.username", "cal.password", "card-cal-2.username", "card-cal-2.password", "unrelated"):
            (store / n).write_text("sealed-blob")
        text = ca.credential_dropin(store, ca.load_registry(self.registry))
        lines = [l for l in text.splitlines() if l.startswith("LoadCredentialEncrypted=")]
        self.assertEqual(sorted(l.split("=", 1)[1].split(":", 1)[0] for l in lines),
                         ["cal.password", "cal.username", "card-cal-2.password", "card-cal-2.username"])
        self.assertNotIn("card-cal-main", text)

    def test_cli_add_and_mirror(self):
        helper = [sys.executable, str(HOST / "card_accounts.py"), "--registry", str(self.registry)]
        self.assertEqual(subprocess.run(helper + ["ensure-default"], capture_output=True).returncode, 0)
        out = subprocess.run(helper + ["fields", "cal-2", "cal"], capture_output=True, text=True)
        self.assertEqual(out.stdout.split(), ["username", "password"])
        bad = subprocess.run(helper + ["fields", "max-1", "max"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("not supported yet", bad.stderr)
        self.assertEqual(subprocess.run(helper + ["add", "cal-2", "cal", "--label", "Cal 2", "--max-runs", "1"],
                                        capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run(helper + ["add", "cal-2", "cal"], capture_output=True).returncode, 0)  # idempotent
        reg = ca.load_registry(self.registry)
        self.assertEqual([(a["id"], a["max_runs_per_day"]) for a in reg["accounts"]], [("cal-main", None), ("cal-2", 1)])
        subprocess.run(helper + ["mirror", str(self.root / "spool" / "accounts.json")], check=True)
        mirror = json.loads((self.root / "spool" / "accounts.json").read_text())
        self.assertEqual([a["max_runs_per_day"] for a in mirror["accounts"]], [2, 1])


class RoutingTests(Base):
    def test_no_account_is_cal_main_old_behaviour(self):
        (self.creds / "cal.username").write_text("caluser")
        (self.creds / "cal.password").write_text("calpass")
        st = self.sync()
        self.assertEqual((st["status"], st["account"], st["provider"]), ("DONE", "cal-main", "cal"))
        self.assertEqual(self.users(), ["caluser"])
        self.assertTrue((self.root / "archive" / "cards" / "cal-main" / f"cal-card-1111-{CUR}.csv").is_file())
        self.assertTrue((self.root / "spool" / "accounts" / "cal-main" / "last-status.json").is_file())
        self.assertTrue((self.root / "spool" / f"status-{st['runId']}.json").is_file())
        self.assertIn("cal_main_card_transactions", st["csv"])
        self.assertTrue(cr.CAL_CSV_RE.match(st["csv"]))
        self.assertEqual(json.loads((self.root / "spool" / "accounts.json").read_text())["accounts"][0]["id"], "cal-main")

    def test_routes_to_the_named_account(self):
        self.two_accounts()
        st = self.sync("cal-2")
        self.assertEqual((st["status"], st["account"]), ("DONE", "cal-2"), st)
        self.assertEqual(self.users(), ["cal2user"])
        a2 = self.root / "archive" / "cards" / "cal-2"
        self.assertTrue((a2 / f"cal-card-3333-{CUR}.csv").is_file())
        self.assertFalse((self.root / "archive" / "cards" / "cal-main").exists() and
                         any((self.root / "archive" / "cards" / "cal-main").glob("cal-card-3333-*")))
        self.assertEqual(oct(a2.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.root / "archive" / "cards").stat().st_mode & 0o777), "0o700")
        last = json.loads((self.root / "spool" / "accounts" / "cal-2" / "last-status.json").read_text())
        self.assertEqual(last["account"], "cal-2")
        self.assertFalse((self.root / "spool" / "accounts" / "cal-main" / "last-status.json").exists())

    def test_refusals_without_login(self):
        self.two_accounts()
        for account, code in (("cal-9", "UNKNOWN_ACCOUNT"), ("BAD!", "BAD_ACCOUNT")):
            st = self.sync(account)
            self.assertEqual(st["status"], code)
        reg = json.loads(self.registry.read_text())
        reg["accounts"].append({"id": "max-1", "provider": "max", "enabled": True})
        reg["accounts"][1]["enabled"] = False
        self.registry.write_text(json.dumps(reg))
        self.assertEqual(self.sync("max-1")["status"], "NOT_SUPPORTED")
        self.assertEqual(self.sync("cal-2")["status"], "ACCOUNT_DISABLED")
        self.registry.write_text("{corrupt")
        self.assertEqual(self.sync("cal-main")["status"], "BAD_REGISTRY")
        self.assertEqual(self.users(), [])
        self.assertFalse((self.root / "spool" / "request.json").exists())

    def test_cal2_not_configured_does_not_use_cal_main_creds(self):
        self.two_accounts()
        (self.creds / "card-cal-2.password").unlink()
        self.assertEqual(self.sync("cal-2")["status"], "NOT_CONFIGURED")
        self.assertEqual(self.users(), [])


class BudgetAndStopTests(Base):
    def test_per_account_daily_limit_is_isolated(self):
        self.two_accounts(max_runs_per_day=1)
        for _ in range(2):
            self.assertEqual(self.sync("cal-main")["status"], "DONE")
        st = self.sync("cal-main")
        self.assertEqual((st["status"], st["runsToday"], st["maxRuns"]), ("DAILY_LIMIT", 2, 2))
        self.assertEqual(self.sync("cal-2")["status"], "DONE")                 # other account unaffected
        st = self.sync("cal-2")
        self.assertEqual((st["status"], st["maxRuns"]), ("DAILY_LIMIT", 1))     # registry max_runs_per_day
        runs = json.loads((self.root / "spool" / "accounts" / "cal-2" / "runs-today.json").read_text())
        self.assertEqual((runs["runs"], runs["max_runs"]), (1, 1))
        total = json.loads((self.root / "spool" / "runs-total.json").read_text())
        self.assertEqual((total["runs_total"], total["max_runs_total"]), (3, 0))
        self.assertEqual(self.users(), ["caluser", "caluser", "cal2user"])

    def test_global_cap(self):
        self.two_accounts()
        self.assertEqual(self.sync("cal-main", CARD_MAX_RUNS_TOTAL="2")["status"], "DONE")
        self.assertEqual(self.sync("cal-2", CARD_MAX_RUNS_TOTAL="2")["status"], "DONE")
        st = self.sync("cal-2", CARD_MAX_RUNS_TOTAL="2")
        self.assertEqual((st["status"], st["runsTotal"], st["maxRunsTotal"]), ("DAILY_LIMIT", 2, 2))
        reg = json.loads(self.registry.read_text())
        reg["max_runs_total"] = 1                                                 # registry cap: the lower wins
        self.registry.write_text(json.dumps(reg))
        self.assertEqual(self.sync("cal-main")["status"], "DAILY_LIMIT")
        self.assertEqual(len(self.users()), 2)


class ProviderStopTests(Base):
    """Bot-detection / 429 / HTML / captcha on ANY Cal account = provider-wide stop for the Israel day;
    auth failures stay per account. Refusals never log in and never count."""
    DAY = 86400

    def runs(self, account):
        p = self.root / "state" / "accounts" / account / f"runs-{cr.base.today()}"
        return int(p.read_text()) if p.exists() else 0

    def pfile(self, where="state"):
        return self.root / where / "providers" / "cal" / "stop-today.json"

    def sync_at(self, account, offset, behaviour="ok"):
        req = {"run_id": os.urandom(8).hex(), "requested_at": time.time() + offset, "actions": OPEN, "account": account}
        (self.root / "spool" / "request.json").write_text(json.dumps(req))
        env = dict(self.env, CAL_RUNNER_TEST_EXEC=json.dumps([sys.executable, "-u", str(FAKE), behaviour, str(self.logins)]))
        return cr.CalRunner(cr.CalConfig(env), clock=lambda: time.time() + offset).run()[0]

    def test_block_on_cal2_stops_cal_main_and_cal2(self):
        for behaviour, reason in (("blocked", "CAL_BLOCKED"), ("cal-blocked", "CAL_BLOCKED"),
                                  ("captcha", "CAPTCHA_OR_BLOCK")):
            with self.subTest(behaviour=behaviour):
                for d in ("state/accounts", "spool/accounts", "state/providers", "spool/providers"):
                    shutil.rmtree(self.root / d, ignore_errors=True)
                self.two_accounts()
                st = self.sync("cal-2", behaviour)
                self.assertEqual(st["providerStop"]["triggered_by"], "cal-2")
                self.assertEqual(st["providerStop"]["reason"], reason)
                state = json.loads(self.pfile().read_text())
                mirror = json.loads(self.pfile("spool").read_text())
                self.assertEqual(state, mirror)
                self.assertEqual(set(state), {"date", "provider", "status", "reason", "triggered_by", "at"})
                self.assertEqual(oct(self.pfile().stat().st_mode & 0o777), "0o600")
                logins, main_runs = len(self.users()), self.runs("cal-main")
                for account in ("cal-main", "cal-2"):
                    st = self.sync(account)
                    self.assertEqual(st["status"], "PROVIDER_STOPPED_TODAY")
                    self.assertEqual((st["providerStop"]["triggered_by"], st["account"]), ("cal-2", account))
                    self.assertNotIn("stopForToday", st)
                self.assertEqual(len(self.users()), logins)                  # no login
                self.assertEqual(self.runs("cal-main"), main_runs)           # no budget consumed
                last = json.loads((self.root / "spool" / "accounts" / "cal-main" / "last-status.json").read_text())
                self.assertEqual(last["providerStop"]["triggered_by"], "cal-2")
                self.assertFalse((self.root / "state" / "accounts" / "cal-main" / f"stop-{cr.base.today()}").exists())

    def test_auth_failure_on_cal2_does_not_stop_cal_main(self):
        self.two_accounts()
        st = self.sync("cal-2", "invalid")
        self.assertEqual(st["status"], "INVALID_PASSWORD")
        self.assertNotIn("providerStop", st)
        self.assertFalse(self.pfile().exists())
        self.assertEqual(self.sync("cal-main")["status"], "DONE")
        self.assertEqual(self.sync("cal-2")["status"], "STOPPED_TODAY")
        self.assertEqual(self.users(), ["cal2user", "caluser"])

    def test_stop_expires_at_israel_date_rollover(self):
        self.two_accounts()
        self.sync("cal-2", "cal-blocked")
        self.assertEqual(self.sync("cal-main")["status"], "PROVIDER_STOPPED_TODAY")
        tomorrow = self.sync_at("cal-main", self.DAY)
        self.assertEqual(tomorrow["status"], "DONE", tomorrow)
        self.assertEqual(self.sync_at("cal-2", self.DAY)["status"], "DONE")    # its own stop expired too
        # the next block (tomorrow) replaces yesterday's file with a fresh trigger
        st = self.sync_at("cal-main", self.DAY, "captcha")
        self.assertEqual(st["providerStop"]["triggered_by"], "cal-main")
        self.assertEqual(json.loads(self.pfile().read_text())["date"], cr.base.today(time.time() + self.DAY))

    def test_auth_stop_and_provider_stop_coexist(self):
        self.two_accounts()
        self.assertEqual(self.sync("cal-main", "invalid")["status"], "INVALID_PASSWORD")
        st = self.sync("cal-2", "cal-blocked")
        self.assertEqual(st["providerStop"]["triggered_by"], "cal-2")
        self.assertTrue((self.root / "state" / "accounts" / "cal-main" / f"stop-{cr.base.today()}").is_file())
        self.assertTrue((self.root / "state" / "accounts" / "cal-2" / f"stop-{cr.base.today()}").is_file())
        self.assertTrue(self.pfile().is_file())
        self.assertEqual(self.sync("cal-main")["status"], "PROVIDER_STOPPED_TODAY")
        # first trigger of the day wins (who/when kept)
        first = json.loads(self.pfile().read_text())
        self.assertEqual(json.loads(self.pfile().read_text()), first)
        self.assertEqual(self.users(), ["caluser", "cal2user"])

    def test_corrupt_provider_state_fails_closed_today(self):
        self.two_accounts()
        self.pfile().parent.mkdir(parents=True)
        self.pfile().write_text("{broken")
        st = self.sync("cal-main")
        self.assertEqual((st["status"], st["providerStop"]["status"]), ("PROVIDER_STOPPED_TODAY", "PROVIDER_STATE_CORRUPT"))
        self.assertEqual(self.users(), [])
        old = time.time() - 2 * self.DAY
        os.utime(self.pfile(), (old, old))
        self.assertEqual(self.sync("cal-main")["status"], "DONE")


class LockTests(Base):
    def hold(self, path):
        h = open(path, "a+")
        fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(h.close)
        return h

    def test_shared_bank_lock_blocks_card_login(self):
        self.two_accounts()
        self.hold(self.root / "hapoalim-state" / "run.lock")      # a Hapoalim run holds its runner lock
        req = self.queue("cal-2")
        status, code = self.runner().run()
        self.assertEqual((status["status"], code, status["account"]), ("LOCKED", 3, "cal-2"))
        self.assertEqual(self.users(), [])
        self.assertFalse((self.root / "spool" / "request.json").exists())
        self.assertTrue((self.root / "spool" / f"status-{req['run_id']}.json").is_file())

    def test_card_lock_and_hapoalim_sees_card_run(self):
        self.two_accounts()
        self.hold(self.root / "state" / "run.lock")
        self.queue("cal-main")
        self.assertEqual(self.runner().run()[0]["status"], "LOCKED")
        # The reverse direction: while a card run holds the bank lock, the Hapoalim runner's own flock fails.
        h = open(self.root / "hapoalim-state" / "run.lock", "a+")
        try:
            fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
            other = open(self.root / "hapoalim-state" / "run.lock", "a+")
            with self.assertRaises(OSError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            other.close()
        finally:
            h.close()

    def test_runner_releases_both_locks(self):
        self.two_accounts()
        self.assertEqual(self.sync("cal-2")["status"], "DONE")
        for p in (self.root / "state" / "run.lock", self.root / "hapoalim-state" / "run.lock"):
            h = open(p, "a+")
            fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
            h.close()

    def test_no_hapoalim_dir_means_no_bank_lock(self):
        (self.creds / "cal.username").write_text("u")
        (self.creds / "cal.password").write_text("p")
        st = self.sync(CARD_BANK_LOCK=str(self.root / "absent" / "run.lock"))
        self.assertEqual(st["status"], "DONE")


class MigrationTests(Base):
    def legacy_layout(self):
        """The single-account layout exactly as the a1a5c34 runner leaves it (synthetic rows)."""
        arch = self.root / "archive"
        payload = {"cards": ["1111", "2222"], "retrieval": {c: {"known": True, "months": [
            {"month": m, "rows": 1, "capped": False} for m in ("2026-02", "2026-03")], "pending": "ok"}
            for c in ("1111", "2222")},
            "rows": [{"card": c, "billing_month": m, "purchase_date": f"{m}-03", "charge_date": f"{m}-10",
                      "amount": -1, "description": "synthetic", "identifier": f"{c}{m}"}
                     for c in ("1111", "2222") for m in ("2026-02", "2026-03")]
            + [{"card": "1111", "billing_month": None, "purchase_date": "2026-03-20", "amount": -2,
                "description": "pending synthetic", "status": "pending"}]}
        cal_archive.merge_sync(arch, payload, run_id="legacy", now=time.time())
        st = self.root / "state"
        (st / f"runs-{cr.base.today()}").write_text("1\n")
        (st / "stop-2026-01-01").write_text('{"status": "INVALID_PASSWORD"}\n')
        sp = self.root / "spool"
        (sp / "last-status.json").write_text(json.dumps({"status": "DONE", "finishedAt": time.time()}))
        (sp / "runs-today.json").write_text(json.dumps({"date": cr.base.today(), "runs": 1, "max_runs": 2}))
        (self.creds / "cal.username").write_text("caluser")
        (self.creds / "cal.password").write_text("calpass")

    def mig(self, *flags):
        return mig.main(list(flags) + ["--archive", str(self.root / "archive"), "--state", str(self.root / "state"),
                                       "--spool", str(self.root / "spool")])

    def test_round_trip_lossless_and_idempotent(self):
        self.legacy_layout()
        before = tree(self.root)
        # Runner refuses to log in while the old layout is still there.
        self.assertEqual(self.sync()["status"], "MIGRATION_PENDING")
        self.assertEqual(self.users(), [])
        self.assertFalse((self.root / "spool" / "accounts").exists())      # nothing created that could conflict
        for p in list((self.root / "spool").glob("status-*.json")) + [self.root / "spool" / "accounts.json"]:
            p.unlink()
        self.assertEqual(tree(self.root), before)
        # dry-run changes nothing
        self.assertEqual(self.mig("--dry-run"), 0)
        self.assertEqual(tree(self.root), before)
        # apply: same bytes, new places
        self.assertEqual(self.mig("--apply"), 0)
        after = tree(self.root)
        moved = {k.replace("archive/", "archive/cards/cal-main/", 1).replace("state/", "state/accounts/cal-main/", 1)
                 .replace("spool/", "spool/accounts/cal-main/", 1): v for k, v in before.items()
                 if k.split("/", 1)[0] in ("archive", "state", "spool")}
        moved.update({k: v for k, v in before.items() if k.split("/", 1)[0] not in ("archive", "state", "spool")})
        self.assertEqual(after, moved)
        manifests = list((self.root / "state" / "migrations").glob("cal-multi-forward-*.json"))
        self.assertEqual(len(manifests), 1)
        man = json.loads(manifests[0].read_text())
        self.assertEqual(len(man["files"]), len([k for k in before if k.split("/", 1)[0] in ("archive", "state", "spool")]))
        self.assertTrue(all(len(f["sha256"]) == 64 for f in man["files"]))
        self.assertEqual(self.mig("--apply"), 0)                                   # idempotent
        self.assertEqual(tree(self.root), after)
        # new syncs after the migration (cal-main + a second account), then rollback
        reg = {"accounts": [{"id": "cal-main", "provider": "cal"}, {"id": "cal-2", "provider": "cal"}]}
        self.registry.write_text(json.dumps(reg))
        (self.creds / "card-cal-2.username").write_text("cal2user")
        (self.creds / "card-cal-2.password").write_text("cal2pass")
        self.assertEqual(self.sync()["status"], "DONE")
        self.assertEqual(self.sync("cal-2")["status"], "DONE")
        new_main = tree(self.root / "archive" / "cards" / "cal-main")
        cal2 = tree(self.root / "archive" / "cards" / "cal-2")
        self.assertIn(f"cal-card-1111-{CUR}.csv", new_main)
        self.assertEqual(self.mig("--rollback-dry-run"), 0)
        self.assertEqual(self.mig("--rollback"), 0)
        arch_after = tree(self.root / "archive")
        for name, digest in new_main.items():                                    # nothing of cal-main lost
            self.assertEqual(arch_after[name], digest)
        for name, digest in cal2.items():                                        # cal-2 untouched
            self.assertEqual(arch_after[f"cards/cal-2/{name}"], digest)
        self.assertFalse((self.root / "archive" / "cards" / "cal-main").exists())
        self.assertFalse((self.root / "state" / "accounts" / "cal-main").exists())
        self.assertTrue((self.root / "spool" / "last-status.json").is_file())
        self.assertTrue((self.root / "state" / f"runs-{cr.base.today()}").is_file())
        self.assertEqual(self.mig("--rollback"), 0)                                # idempotent
        # re-apply again
        self.assertEqual(self.mig("--apply"), 0)
        st = self.sync()                       # the migrated counter is honoured: 1 legacy run + 1 after = 2/2
        self.assertEqual((st["status"], st["runsToday"]), ("DAILY_LIMIT", 2))
        self.assertEqual(self.sync("cal-2")["status"], "DONE")

    def test_conflict_moves_nothing(self):
        self.legacy_layout()
        d = self.root / "archive" / "cards" / "cal-main"
        d.mkdir(parents=True)
        (d / "_coverage.json").write_text("{}")
        before = tree(self.root)
        self.assertEqual(self.mig("--apply"), 3)
        self.assertEqual(tree(self.root), before)

    def test_identical_duplicate_is_merged(self):
        self.legacy_layout()
        src = self.root / "archive" / "_coverage.json"
        d = self.root / "archive" / "cards" / "cal-main"
        d.mkdir(parents=True)
        (d / "_coverage.json").write_bytes(src.read_bytes())
        self.assertEqual(self.mig("--apply"), 0)
        self.assertFalse(src.exists())
        self.assertTrue((d / "_coverage.json").is_file())

    def test_lock_held_refuses(self):
        self.legacy_layout()
        h = open(self.root / "state" / "run.lock", "a+")
        fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertEqual(self.mig("--apply"), 4)
        finally:
            h.close()

    def test_cc_copy_identical(self):
        self.assertEqual((HOST / "card_accounts.py").read_bytes(),
                         (HERE.parent / "command-center" / "command_center" / "card_accounts.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
