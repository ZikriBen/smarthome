#!/usr/bin/env python3
"""Offline tests for smarthome-secrets (no network, no real systemd needed).

Run:  python3 secrets/test/test_smarthome_secrets.py -v
      sudo -E python3 secrets/test/test_smarthome_secrets.py -v   (adds owner + real systemd-creds tests)
Every subprocess output is checked: no dummy secret value may ever be printed.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.dirname(HERE)
REPO = os.path.dirname(SECRETS)
TOOL = os.path.join(SECRETS, "bin", "smarthome_secrets.py")
SHIM = os.path.join(SECRETS, "bin", "secrets-env-shim")
MOCK = os.path.join(HERE, "mock-systemd-creds")
IS_ROOT = os.geteuid() == 0

# Dummy values only. Distinctive so a leak is easy to grep for.
DUMMY = {
    "SEARXNG_SECRET": "DUMMY-searx-7f3a9c1e",
    "OPENAI_API_KEY": "DUMMY-openai-55aa",
    "TELEGRAM_BOT_TOKEN": "DUMMY-tgbot-9911",
    "TELEGRAM_ALLOWED_USERS": "12345",
    "COMMAND_CENTER_TOKEN": "DUMMY-cc-token-31",
    "COMMAND_CENTER_CALLBACK_TOKEN": "DUMMY-cc-cb-77",
    "UPTIME_KUMA_API_KEY": "DUMMY-kuma-12",
    "JELLYFIN_API_KEY": "DUMMY-jf-0042",
    "BESZEL_TOKEN": "DUMMY-beszel-tok-8",
    "SECRET_ENCRYPTION_KEY": "DUMMY-homarr-key-64",
    "IMAP_USER": "dummy.user.mailbox@example.invalid",
    "IMAP_PASS": "DUMMY-imap pass with spaces",
}
LEAK_STRINGS = [v for k, v in DUMMY.items() if k != "TELEGRAM_ALLOWED_USERS"] + [
    "DUMMY-stdin-value-1", "DUMMY-argv-value-2", "DUMMY-other-3", "DUMMY-changed-4",
    "DUMMY-real-creds-5", "DUMMY-sq-6", "DUMMY-dq\"7",
]
ALL_OUTPUT = []


def compose_cmd():
    for cmd in (["docker", "compose"], ["docker-compose"]):
        try:
            if subprocess.run(cmd + ["version"], capture_output=True).returncode == 0:
                return cmd
        except FileNotFoundError:
            pass
    return None


COMPOSE = compose_cmd()


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


class Env:
    """A throw-away root: etc, credstore, rundir (on /dev/shm = tmpfs), fake repo."""

    def __init__(self, creds=MOCK, require_tmpfs="0"):
        self.tmp = tempfile.mkdtemp(prefix="sms-test-")
        self.etc = os.path.join(self.tmp, "etc")
        self.credstore = os.path.join(self.tmp, "credstore", "smarthome")
        shm = "/dev/shm" if os.path.isdir("/dev/shm") else self.tmp
        self.rundir = tempfile.mkdtemp(prefix="sms-run-", dir=shm)
        os.rmdir(self.rundir)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.etc)
        os.makedirs(self.repo)
        shutil.copy(os.path.join(SECRETS, "manifest.yaml"), os.path.join(self.etc, "manifest.yaml"))
        with open(os.path.join(self.etc, "config"), "w") as fh:
            fh.write(f"REPO_ROOT={self.repo}\nWITH_KEY=host\nCOMPOSE_CMD={' '.join(COMPOSE or ['false'])}\n")
        self.env = dict(os.environ)
        self.env.update(
            SMARTHOME_SECRETS_TEST="1",
            SMARTHOME_SECRETS_ETC=self.etc,
            SMARTHOME_SECRETS_CREDSTORE=self.credstore,
            SMARTHOME_SECRETS_RUNDIR=self.rundir,
            SMARTHOME_SECRETS_SYSTEMD_CREDS=creds,
            SMARTHOME_SECRETS_REQUIRE_TMPFS=require_tmpfs,
        )
        for k in list(self.env):
            if k.startswith("MOCK_CREDS_") or k == "COMPOSE_FILE":
                del self.env[k]

    def run(self, prog, *args, stdin=b"", extra_env=None, check=None):
        env = dict(self.env)
        env.update(extra_env or {})
        p = subprocess.run([sys.executable, TOOL, prog, *args], input=stdin, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = p.stdout.decode(errors="replace") + p.stderr.decode(errors="replace")
        ALL_OUTPUT.append(out)
        for s in LEAK_STRINGS:
            assert s not in out, f"secret value leaked in output of {prog} {args}"
        if check is not None:
            assert p.returncode == check, f"{prog} {args}: rc={p.returncode}\n{out}"
        return p.returncode, out

    def copy_repo(self, *rel_dirs):
        for d in rel_dirs:
            src = os.path.join(REPO, d)
            dst = os.path.join(self.repo, d)
            os.makedirs(dst, exist_ok=True)
            for f in os.listdir(src):
                if f.startswith("compose") or f.startswith("docker-compose"):
                    shutil.copy(os.path.join(src, f), dst)

    def write_env(self, rel, text):
        p = os.path.join(self.repo, rel)
        with open(p, "w") as fh:
            fh.write(text)
        os.chmod(p, 0o640)
        return p

    def cleanup(self):
        if os.path.isdir(self.rundir):
            for root, dirs, _ in os.walk(self.rundir):
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o700)
            os.chmod(self.rundir, 0o700)
            shutil.rmtree(self.rundir, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


def home_agent_env(**over):
    vals = dict(DUMMY)
    vals.update(over)
    keys = ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "SEARXNG_SECRET",
            "COMMAND_CENTER_TOKEN", "COMMAND_CENTER_CALLBACK_TOKEN", "UPTIME_KUMA_API_KEY", "JELLYFIN_API_KEY"]
    return "".join(f"{k}={vals[k]}\n" for k in keys)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.argv_log = os.path.join(self.e.tmp, "argv.log")

    def tearDown(self):
        self.e.cleanup()

    def test_set_list_rm_names_only(self):
        self.e.run("smarthome-secret", "set", "searxng", "SEARXNG_SECRET", stdin=b"DUMMY-stdin-value-1\n",
                   extra_env={"MOCK_CREDS_ARGV_LOG": self.argv_log}, check=0)
        cred = os.path.join(self.e.credstore, "searxng.SEARXNG_SECRET")
        self.assertTrue(os.path.isfile(cred))
        self.assertEqual(stat.S_IMODE(os.stat(cred).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.e.credstore).st_mode), 0o700)
        self.assertNotIn(b"DUMMY-stdin-value-1", read_bytes(cred))
        self.assertNotIn(b"DUMMY-stdin-value-1", read_bytes(self.argv_log))  # never in argv
        rc, out = self.e.run("smarthome-secret", "list", check=0)
        self.assertEqual(out.strip(), "searxng SEARXNG_SECRET")
        rc, out = self.e.run("smarthome-secret", "list", "searxng", check=0)
        self.assertIn("SEARXNG_SECRET", out)
        rc, out = self.e.run("smarthome-secret", "list", "homarr", check=0)
        self.assertEqual(out.strip(), "")
        self.e.run("smarthome-secret", "rm", "searxng", "SEARXNG_SECRET", check=0)
        self.assertFalse(os.path.exists(cred))
        self.e.run("smarthome-secret", "rm", "searxng", "SEARXNG_SECRET", check=1)

    def test_one_trailing_newline_stripped(self):
        self.e.run("smarthome-secret", "set", "searxng", "A", stdin=b"DUMMY-other-3\n\n", check=0)
        self.e.run("smarthome-secrets", "apply", check=0)
        with open(os.path.join(self.e.rundir, "searxng", "A"), "rb") as fh:
            self.assertEqual(fh.read(), b"DUMMY-other-3\n")

    def test_value_on_argv_is_rejected_and_not_echoed(self):
        rc, out = self.e.run("smarthome-secret", "set", "searxng", "SEARXNG_SECRET", "DUMMY-argv-value-2",
                             stdin=b"DUMMY-stdin-value-1\n")
        self.assertEqual(rc, 1)
        self.assertIn("never accepted on the command line", out)
        self.assertFalse(os.path.exists(self.e.credstore) and os.listdir(self.e.credstore))

    def test_validation(self):
        bad = [("../etc", "X"), ("searxng", "../X"), ("Searxng", "X"), ("a.b", "X"), ("searxng", "x"),
               ("searxng", "A.B"), ("searxng", "A/B"), ("", "X"), ("searxng", "A" * 65), ("-x", "X")]
        for svc, name in bad:
            rc, out = self.e.run("smarthome-secret", "set", svc, name, stdin=b"DUMMY-other-3")
            self.assertEqual(rc, 1, (svc, name))
            self.assertIn("invalid", out)
        self.assertFalse(os.path.isdir(self.e.credstore) and os.listdir(self.e.credstore))

    def test_empty_and_nul_rejected(self):
        self.e.run("smarthome-secret", "set", "searxng", "A", stdin=b"\n", check=1)
        self.e.run("smarthome-secret", "set", "searxng", "A", stdin=b"ab\0cd", check=1)

    def test_non_root_refused_outside_test_mode(self):
        if IS_ROOT:
            self.skipTest("running as root")
        env = dict(os.environ)
        env.pop("SMARTHOME_SECRETS_TEST", None)
        p = subprocess.run([sys.executable, TOOL, "smarthome-secret", "list"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("must run as root", p.stderr)


class TestApply(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def _set(self, svc, name, value):
        self.e.run("smarthome-secret", "set", svc, name, stdin=value.encode(), check=0)

    def test_permissions_orphans_idempotent(self):
        self._set("searxng", "SEARXNG_SECRET", DUMMY["SEARXNG_SECRET"])
        self._set("homarr", "SECRET_ENCRYPTION_KEY", DUMMY["SECRET_ENCRYPTION_KEY"])
        # orphans: a stale svc dir, a stale file, a leftover tmp file, a stray top-level file
        os.makedirs(os.path.join(self.e.rundir, "gone"), mode=0o700)
        open(os.path.join(self.e.rundir, "gone", "OLD"), "w").close()
        os.makedirs(os.path.join(self.e.rundir, "searxng"), mode=0o700)
        open(os.path.join(self.e.rundir, "searxng", "STALE"), "w").close()
        open(os.path.join(self.e.rundir, "searxng", ".tmp-X.1"), "w").close()
        open(os.path.join(self.e.rundir, "stray"), "w").close()
        for _ in range(2):  # idempotent
            rc, out = self.e.run("smarthome-secrets", "apply", check=0)
            self.assertEqual(sorted(os.listdir(self.e.rundir)), ["homarr", "searxng"])
            self.assertEqual(os.listdir(os.path.join(self.e.rundir, "searxng")), ["SEARXNG_SECRET"])
            self.assertEqual(stat.S_IMODE(os.stat(self.e.rundir).st_mode), 0o700)
            for svc, name in (("searxng", "SEARXNG_SECRET"), ("homarr", "SECRET_ENCRYPTION_KEY")):
                d = os.path.join(self.e.rundir, svc)
                p = os.path.join(d, name)
                self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o500)
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o400)
                with open(p) as fh:
                    self.assertEqual(fh.read(), DUMMY[name])
        self.assertIn("orphan", out)
        # rm -> refresh removes the runtime file and the empty svc dir
        self.e.run("smarthome-secret", "rm", "homarr", "SECRET_ENCRYPTION_KEY", check=0)
        self.e.run("smarthome-secrets", "refresh", check=0)
        self.assertEqual(os.listdir(self.e.rundir), ["searxng"])
        rc, out = self.e.run("smarthome-secrets", "status", check=0)
        self.assertIn("searxng SEARXNG_SECRET runtime=0o400", out)

    def test_decrypt_failure_continues_and_exits_nonzero(self):
        self._set("searxng", "SEARXNG_SECRET", DUMMY["SEARXNG_SECRET"])
        self._set("homarr", "SECRET_ENCRYPTION_KEY", DUMMY["SECRET_ENCRYPTION_KEY"])
        rc, out = self.e.run("smarthome-secrets", "apply",
                             extra_env={"MOCK_CREDS_FAIL": "searxng.SEARXNG_SECRET"})
        self.assertEqual(rc, 1)
        self.assertIn("FAILED searxng.SEARXNG_SECRET", out)
        self.assertTrue(os.path.isfile(os.path.join(self.e.rundir, "homarr", "SECRET_ENCRYPTION_KEY")))
        self.assertFalse(os.path.exists(os.path.join(self.e.rundir, "searxng", "SEARXNG_SECRET")))

    def test_failure_keeps_previous_runtime_file(self):
        self._set("searxng", "SEARXNG_SECRET", DUMMY["SEARXNG_SECRET"])
        self.e.run("smarthome-secrets", "apply", check=0)
        self.e.run("smarthome-secrets", "apply", extra_env={"MOCK_CREDS_FAIL": "searxng.SEARXNG_SECRET"}, check=1)
        self.assertTrue(os.path.isfile(os.path.join(self.e.rundir, "searxng", "SEARXNG_SECRET")))

    def test_tampered_name_fails(self):
        self._set("searxng", "SEARXNG_SECRET", DUMMY["SEARXNG_SECRET"])
        os.rename(os.path.join(self.e.credstore, "searxng.SEARXNG_SECRET"),
                  os.path.join(self.e.credstore, "homarr.SECRET_ENCRYPTION_KEY"))
        rc, out = self.e.run("smarthome-secrets", "apply")
        self.assertEqual(rc, 1)  # name bound into the credential (like real systemd-creds)

    def test_refuses_non_tmpfs(self):
        e = Env(require_tmpfs="1")
        try:
            e.rundir = os.path.join(e.tmp, "run-on-disk")
            e.env["SMARTHOME_SECRETS_RUNDIR"] = e.rundir
            e.run("smarthome-secret", "set", "searxng", "A", stdin=b"DUMMY-other-3", check=0)
            rc, out = e.run("smarthome-secrets", "apply")
            if subprocess.run(["stat", "-f", "-c", "%T", e.tmp], capture_output=True, text=True).stdout.strip() == "tmpfs":
                self.skipTest("TMPDIR is tmpfs")
            self.assertEqual(rc, 1)
            self.assertIn("not on tmpfs", out)
        finally:
            e.cleanup()

    @unittest.skipUnless(IS_ROOT, "owner mapping needs root")
    def test_owner_mapping(self):
        with open(os.path.join(self.e.etc, "owners.conf"), "w") as fh:
            fh.write("* 0:0\nsearxng 977:977\n")
        self._set("searxng", "SEARXNG_SECRET", DUMMY["SEARXNG_SECRET"])
        self._set("homarr", "SECRET_ENCRYPTION_KEY", DUMMY["SECRET_ENCRYPTION_KEY"])
        self.e.run("smarthome-secrets", "apply", check=0)
        st = os.stat(os.path.join(self.e.rundir, "searxng", "SEARXNG_SECRET"))
        self.assertEqual((st.st_uid, st.st_gid), (977, 977))
        st = os.stat(os.path.join(self.e.rundir, "homarr", "SECRET_ENCRYPTION_KEY"))
        self.assertEqual((st.st_uid, st.st_gid), (0, 0))

    def test_bad_owners_conf(self):
        with open(os.path.join(self.e.etc, "owners.conf"), "w") as fh:
            fh.write("searxng root\n")
        self._set("searxng", "A", "DUMMY-other-3")
        self.e.run("smarthome-secrets", "apply", check=1)


class TestShim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shim-")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def f(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def shim(self, env, cmd, sh="sh"):
        base = {"PATH": os.environ["PATH"]}
        base.update(env)
        p = subprocess.run([sh, SHIM] + cmd, env=base, capture_output=True, text=True)
        for s in LEAK_STRINGS:
            self.assertNotIn(s, p.stderr)
        return p

    def shells(self):
        out = ["sh"]
        if shutil.which("dash"):
            out.append("dash")
        if shutil.which("busybox"):
            out.append("busybox sh")
        return out

    def test_allowlist_newline_unset(self):
        a = self.f("a", b"DUMMY-other-3\n")
        b = self.f("b", b"DUMMY-changed-4\n\n")
        legit = self.f("legit", b"/some/config/path\n")
        for sh in self.shells():
            p = self.shim({"SECRETS_FROM_FILES": "AAA_KEY,BBB_KEY", "AAA_KEY_FILE": a, "BBB_KEY_FILE": b,
                           "LEGIT_FILE": legit},
                          ["env"], sh=sh)
            self.assertEqual(p.returncode, 0, p.stderr)
            env = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
            self.assertEqual(env["AAA_KEY"], "DUMMY-other-3")
            self.assertNotIn("AAA_KEY_FILE", env)
            self.assertNotIn("SECRETS_FROM_FILES", env)
            self.assertEqual(env["LEGIT_FILE"], legit)  # not allowlisted: untouched
            self.assertNotIn("LEGIT", env)               # ...and nothing derived from it
            self.assertIn("loaded AAA_KEY", p.stderr)
        # exactly ONE trailing newline is stripped
        p = self.shim({"SECRETS_FROM_FILES": "BBB_KEY", "BBB_KEY_FILE": b},
                      ["sh", "-c", 'printf "%s|" "$BBB_KEY"'])
        self.assertEqual(p.stdout, "DUMMY-changed-4\n|")

    def test_value_with_spaces_and_specials(self):
        a = self.f("a", b"DUMMY-imap pass with spaces $HOME `x` 'q'\n")
        p = self.shim({"SECRETS_FROM_FILES": "IMAP_PASS", "IMAP_PASS_FILE": a},
                      ["sh", "-c", 'test "$IMAP_PASS" = "DUMMY-imap pass with spaces \\$HOME \\`x\\` '"'q'"'" && echo MATCH'])
        self.assertEqual(p.stdout.strip(), "MATCH", p.stderr)

    def test_fail_closed(self):
        empty = self.f("e", b"\n")
        cases = [
            ({"SECRETS_FROM_FILES": "X"}, 64),                                   # X_FILE unset
            ({"SECRETS_FROM_FILES": "X", "X_FILE": "/nonexistent/x"}, 66),       # unreadable
            ({"SECRETS_FROM_FILES": "X", "X_FILE": empty}, 65),                  # empty
            ({"SECRETS_FROM_FILES": "x;id", "X_FILE": empty}, 64),               # bad name
            ({"SECRETS_FROM_FILES": "A$(id)", "X_FILE": empty}, 64),
        ]
        for env, rc in cases:
            p = self.shim(env, ["true"])
            self.assertEqual(p.returncode, rc, (env, p.stderr))
        p = self.shim({}, [])
        self.assertEqual(p.returncode, 64)

    def test_no_allowlist_is_passthrough(self):
        p = self.shim({"FOO_FILE": "/x"}, ["sh", "-c", 'echo "${FOO-unset}|$FOO_FILE"'])
        self.assertEqual(p.stdout.strip(), "unset|/x")

    def test_exec_preserves_pid(self):
        a = self.f("a", b"DUMMY-other-3")
        proc = subprocess.Popen(["sh", SHIM, "sh", "-c", "echo $$"], stdout=subprocess.PIPE, text=True,
                                env={"PATH": os.environ["PATH"], "SECRETS_FROM_FILES": "A", "A_FILE": a},
                                stderr=subprocess.DEVNULL)
        out, _ = proc.communicate()
        self.assertEqual(int(out.strip()), proc.pid)


@unittest.skipUnless(COMPOSE, "docker compose not available")
class TestComposeRender(unittest.TestCase):
    """`docker compose config` of the base + overrides (dummy .env, rendered in memory)."""

    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def render(self, rel, files, env_text):
        d = os.path.join(self.e.repo, rel)
        self.e.write_env(os.path.join(rel, ".env"), env_text)
        env = {"PATH": os.environ["PATH"], "HOME": self.e.tmp}
        if files:
            env["COMPOSE_FILE"] = ":".join(files)
        p = subprocess.run(COMPOSE + ["config", "--format", "json"], cwd=d, env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["services"], json.loads(p.stdout).get("secrets", {})

    def test_searxng_pilot(self):
        self.e.copy_repo("home-agent")
        # before migration (base only, value still in .env): unchanged behaviour
        svcs, _ = self.render("home-agent", None, home_agent_env())
        self.assertEqual(svcs["searxng"]["environment"]["SEARXNG_SECRET"], DUMMY["SEARXNG_SECRET"])
        # after migration: key removed from .env, override active
        env_text = home_agent_env().replace(f"SEARXNG_SECRET={DUMMY['SEARXNG_SECRET']}\n", "")
        svcs, secrets = self.render("home-agent", ["compose.yaml", "compose.secrets.searxng.yaml"], env_text)
        s = svcs["searxng"]
        self.assertNotIn("SEARXNG_SECRET", s["environment"])
        self.assertEqual(s["environment"]["SEARXNG_SECRET_FILE"], "/run/secrets/SEARXNG_SECRET")
        self.assertEqual(s["environment"]["SECRETS_FROM_FILES"], "SEARXNG_SECRET")
        self.assertEqual(s["entrypoint"], ["/usr/local/bin/secrets-env-shim", "/usr/local/searxng/entrypoint.sh"])
        self.assertEqual(s["secrets"], [{"source": "searxng_SEARXNG_SECRET", "target": "SEARXNG_SECRET"}])
        self.assertEqual(secrets["searxng_SEARXNG_SECRET"]["file"], "/run/smarthome-secrets/searxng/SEARXNG_SECRET")
        self.assertTrue(any(v.get("target") == "/usr/local/bin/secrets-env-shim" and v.get("read_only")
                            for v in s["volumes"]))
        # defence in depth: even if the key is still in .env, !reset keeps it out of the container
        svcs, _ = self.render("home-agent", ["compose.yaml", "compose.secrets.searxng.yaml"], home_agent_env())
        self.assertNotIn("SEARXNG_SECRET", svcs["searxng"]["environment"])
        # unset everywhere and no override -> not passed at all (SearXNG then refuses to start)
        svcs, _ = self.render("home-agent", None, env_text)
        self.assertIsNone((svcs["searxng"]["environment"] or {}).get("SEARXNG_SECRET"))
        # other services untouched by the pilot
        self.assertEqual(svcs["hermes"]["environment"]["OPENAI_API_KEY"], DUMMY["OPENAI_API_KEY"])

    def test_home_agent_later_overrides(self):
        """The documented later state: base ${X:?} lines converted to pass-through, keys
        gone from .env, all three overrides active. (Does not rely on `!reset`, which
        compose 2.39 honours only in the FIRST override file of a COMPOSE_FILE chain.)"""
        import re
        self.e.copy_repo("home-agent")
        later = ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "COMMAND_CENTER_TOKEN",
                 "COMMAND_CENTER_CALLBACK_TOKEN", "UPTIME_KUMA_API_KEY", "JELLYFIN_API_KEY"]
        base = os.path.join(self.e.repo, "home-agent", "compose.yaml")
        with open(base) as fh:
            text = fh.read()
        for n in later:
            text, k = re.subn(r"(?m)^(\s+)" + n + r": \$\{" + n + r":\?[^}]*\}$", r"\1" + n + ":", text)
            self.assertGreaterEqual(k, 1, n)
        with open(base, "w") as fh:
            fh.write(text)
        env_text = "".join(l for l in home_agent_env().splitlines(True)
                           if l.split("=")[0] not in later + ["SEARXNG_SECRET"])
        files = ["compose.yaml", "compose.secrets.searxng.yaml", "compose.secrets.command-center.yaml",
                 "compose.secrets.hermes.yaml"]
        svcs, secrets = self.render("home-agent", files, env_text)
        h = svcs["hermes"]
        for var in ("OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "COMMAND_CENTER_CALLBACK_TOKEN"):
            self.assertIsNone(h["environment"].get(var))
            self.assertIn(var + "_FILE", h["environment"])
        self.assertEqual(h["environment"]["SECRETS_FROM_FILES"],
                         "OPENAI_API_KEY TELEGRAM_BOT_TOKEN COMMAND_CENTER_CALLBACK_TOKEN")
        self.assertEqual(h["entrypoint"], ["/usr/local/bin/secrets-env-shim", "/usr/local/bin/hermes-network-guard"])
        self.assertEqual(h["command"], ["gateway", "run"])
        self.assertEqual(h["environment"]["TELEGRAM_ALLOWED_USERS"], "12345")
        cc = svcs["command-center"]
        self.assertEqual(cc["command"], ["python", "/app/app.py"])
        self.assertIsNone(cc["environment"].get("JELLYFIN_API_KEY"))
        mcp = svcs["command-center-mcp"]
        self.assertEqual(mcp["command"], ["python", "/app/mcp-server.py"])
        self.assertIsNone(mcp["environment"].get("COMMAND_CENTER_TOKEN"))

    def test_other_stacks(self):
        self.e.copy_repo("beszel", "homarr", "mail_rss_service", "telegram-downloader", "telegram-browser")
        svcs, _ = self.render("beszel", ["compose.yaml", "compose.secrets.yaml"], "BESZEL_KEY=ssh-ed25519 PUB\n")
        b = svcs["beszel-agent"]["environment"]
        self.assertNotIn("TOKEN", b)
        self.assertEqual(b["TOKEN_FILE"], "/run/secrets/BESZEL_TOKEN")
        self.assertEqual(b["KEY"], "ssh-ed25519 PUB")
        svcs, _ = self.render("homarr", ["compose.yaml", "compose.secrets.yaml"], "")
        self.assertNotIn("SECRET_ENCRYPTION_KEY", svcs["homarr"]["environment"])
        self.assertIsNone(svcs["homarr"].get("entrypoint"))
        open(os.path.join(self.e.repo, "mail_rss_service", "Dockerfile"), "w").close()
        svcs, _ = self.render("mail_rss_service", ["compose.yaml", "compose.secrets.yaml"], "IMAP_HOST=imap.example\n")
        m = svcs["mail-rss-service"]
        self.assertEqual(m["environment"]["IMAP_HOST"], "imap.example")
        self.assertNotIn("IMAP_PASS", m["environment"])
        self.assertEqual(m["command"][0], "uv")
        svcs, _ = self.render("telegram-downloader", ["compose.yaml", "compose.secrets.yaml"], "MEDIA_ROOT=/media\n")
        self.assertEqual(svcs["telegram-downloader"]["command"], ["python", "-m", "app.main"])
        svcs, _ = self.render("telegram-browser", ["compose.yaml", "compose.secrets.yaml"], "TELEGRAM_SESSION=/data/x\n")
        t = svcs["telegram-browser"]["environment"]
        self.assertEqual(t["JELLYFIN_API_KEY_FILE"], "/run/secrets/tgstream/jellyfin_api_key")  # tg-stream kept
        self.assertEqual(t["SECRETS_FROM_FILES"], "TELEGRAM_API_ID TELEGRAM_API_HASH")


@unittest.skipUnless(COMPOSE, "docker compose not available")
class TestMigrate(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.copy_repo("home-agent", "beszel", "homarr", "mail_rss_service")
        open(os.path.join(self.e.repo, "mail_rss_service", "Dockerfile"), "w").close()
        self.original = "# home-agent\n" + home_agent_env() + "# trailing comment\n"
        self.envp = self.e.write_env("home-agent/.env", self.original)

    def tearDown(self):
        self.e.cleanup()

    def read(self, p=None):
        with open(p or self.envp) as fh:
            return fh.read()

    def test_dry_run_changes_nothing(self):
        rc, out = self.e.run("smarthome-secrets-migrate", "searxng", "--dry-run", check=0)
        self.assertIn("store from .env : SEARXNG_SECRET", out)
        self.assertEqual(self.read(), self.original)
        self.assertFalse(os.path.exists(self.e.credstore) and os.listdir(self.e.credstore))

    def test_migrate_idempotent_and_rollback_exact(self):
        rc, out = self.e.run("smarthome-secrets-migrate", "searxng", check=0)
        self.assertIn("docker compose up -d searxng", out)
        self.assertIn("migrated vars are no longer rendered", out)
        after = self.read()
        self.assertNotIn("SEARXNG_SECRET", after)
        self.assertIn("COMPOSE_FILE=compose.yaml:compose.secrets.searxng.yaml\n", after)
        self.assertIn(f"OPENAI_API_KEY={DUMMY['OPENAI_API_KEY']}", after)  # others untouched
        self.assertEqual(stat.S_IMODE(os.stat(self.envp).st_mode), 0o640)   # mode preserved
        for b in (".pre-secrets", ".pre-secrets.searxng"):
            bp = self.envp + b
            self.assertEqual(stat.S_IMODE(os.stat(bp).st_mode), 0o600)
            self.assertEqual(self.read(bp), self.original)
        with open(os.path.join(self.e.rundir, "searxng", "SEARXNG_SECRET")) as fh:
            self.assertEqual(fh.read(), DUMMY["SEARXNG_SECRET"])
        # idempotent
        rc, out = self.e.run("smarthome-secrets-migrate", "searxng", check=0)
        self.assertIn("already stored  : SEARXNG_SECRET", out)
        self.assertEqual(self.read(), after)
        # rollback -> byte-identical .env (line order aside: restored key is appended)
        self.e.run("smarthome-secrets-rollback", "searxng", check=0)
        restored = self.read()
        self.assertNotIn("COMPOSE_FILE", restored)
        self.assertEqual(sorted(restored.splitlines()), sorted(self.original.splitlines()))
        self.e.run("smarthome-secrets-rollback", "searxng", check=0)   # idempotent
        self.assertEqual(self.read(), restored)
        self.assertTrue(os.path.exists(os.path.join(self.e.credstore, "searxng.SEARXNG_SECRET")))
        self.e.run("smarthome-secrets-rollback", "searxng", "--purge", check=0)
        self.assertFalse(os.path.exists(os.path.join(self.e.credstore, "searxng.SEARXNG_SECRET")))

    def test_rollback_without_backup_uses_credential(self):
        self.e.run("smarthome-secrets-migrate", "searxng", check=0)
        for b in (".pre-secrets", ".pre-secrets.searxng"):
            os.unlink(self.envp + b)
        self.e.run("smarthome-secrets-rollback", "searxng", check=0)
        self.assertIn(f"SEARXNG_SECRET={DUMMY['SEARXNG_SECRET']}\n", self.read())

    def test_existing_compose_file_preserved(self):
        self.e.write_env("home-agent/.env", "COMPOSE_FILE=compose.yaml\n" + self.original)
        self.e.run("smarthome-secrets-migrate", "searxng", check=0)
        self.assertIn("COMPOSE_FILE=compose.yaml:compose.secrets.searxng.yaml\n", self.read())
        self.assertNotIn("added by smarthome-secrets-migrate", self.read())
        self.e.run("smarthome-secrets-rollback", "searxng", check=0)
        self.assertIn("COMPOSE_FILE=compose.yaml\n", self.read())

    def test_needs_review_refused_and_required_interpolation_blocks(self):
        rc, out = self.e.run("smarthome-secrets-migrate", "command-center", check=1)
        self.assertIn("needs-review", out)
        rc, out = self.e.run("smarthome-secrets-migrate", "command-center", "--force", check=1)
        self.assertIn("REQUIRED interpolation", out)
        self.assertEqual(self.read(), self.original)
        rc, out = self.e.run("smarthome-secrets-migrate", "hermes-internal", "--force", check=1)
        self.assertIn("plan-only", out)
        rc, out = self.e.run("smarthome-secrets-migrate", "nope", check=1)

    def test_different_existing_value_needs_overwrite(self):
        self.e.run("smarthome-secret", "set", "searxng", "SEARXNG_SECRET", stdin=b"DUMMY-changed-4", check=0)
        rc, out = self.e.run("smarthome-secrets-migrate", "searxng", check=1)
        self.assertIn("DIFFERENT value", out)
        self.assertEqual(self.read(), self.original)
        self.e.run("smarthome-secrets-migrate", "searxng", "--overwrite", check=0)
        with open(os.path.join(self.e.rundir, "searxng", "SEARXNG_SECRET")) as fh:
            self.assertEqual(fh.read(), DUMMY["SEARXNG_SECRET"])

    def test_decrypt_failure_leaves_env_untouched(self):
        rc, out = self.e.run("smarthome-secrets-migrate", "searxng",
                             extra_env={"MOCK_CREDS_FAIL": "searxng.SEARXNG_SECRET"}, check=1)
        self.assertEqual(self.read(), self.original)

    def test_quoting_variants(self):
        cases = [
            ("SEARXNG_SECRET='DUMMY-sq-6'\n", "DUMMY-sq-6"),
            ('SEARXNG_SECRET="DUMMY-dq\\"7"\n', 'DUMMY-dq"7'),
            ("SEARXNG_SECRET=DUMMY-other-3 # comment\n", "DUMMY-other-3"),
            ("export SEARXNG_SECRET=DUMMY-other-3\n", "DUMMY-other-3"),
        ]
        for line, expect in cases:
            e = Env()
            try:
                e.copy_repo("home-agent")
                text = home_agent_env().replace(f"SEARXNG_SECRET={DUMMY['SEARXNG_SECRET']}\n", line)
                p = e.write_env("home-agent/.env", text)
                e.run("smarthome-secrets-migrate", "searxng", check=0)
                with open(os.path.join(e.rundir, "searxng", "SEARXNG_SECRET")) as fh:
                    self.assertEqual(fh.read(), expect, line)
                e.run("smarthome-secrets-rollback", "searxng", check=0)
                with open(p) as fh:
                    self.assertIn(line, fh.read())  # original line restored verbatim
            finally:
                e.cleanup()
        e = Env()
        try:
            e.copy_repo("home-agent")
            p = e.write_env("home-agent/.env", home_agent_env().replace(
                f"SEARXNG_SECRET={DUMMY['SEARXNG_SECRET']}", "SEARXNG_SECRET=abc${HOME}"))
            rc, out = e.run("smarthome-secrets-migrate", "searxng", check=1)
            self.assertIn("'$'", out)
        finally:
            e.cleanup()

    def test_native_and_env_file_stacks(self):
        bz = self.e.write_env("beszel/.env", f"BESZEL_KEY=ssh-ed25519 PUB\nBESZEL_TOKEN={DUMMY['BESZEL_TOKEN']}\n")
        rc, out = self.e.run("smarthome-secrets-migrate", "beszel-agent", check=0)
        self.assertIn("note: BESZEL_TOKEN is interpolated", out)
        self.assertEqual(self.read(bz), "BESZEL_KEY=ssh-ed25519 PUB\n" + "# smarthome-secrets: COMPOSE_FILE added by "
                         "smarthome-secrets-migrate (rollback removes it)\nCOMPOSE_FILE=compose.yaml:compose.secrets.yaml\n")
        hm = self.e.write_env("homarr/.env", f"SECRET_ENCRYPTION_KEY={DUMMY['SECRET_ENCRYPTION_KEY']}\n")
        self.e.run("smarthome-secrets-migrate", "homarr", check=0)
        ml = self.e.write_env("mail_rss_service/.env",
                              f"IMAP_HOST=imap.example\nIMAP_USER={DUMMY['IMAP_USER']}\nIMAP_PASS=\"{DUMMY['IMAP_PASS']}\"\nPOLL_INTERVAL=60\n")
        self.e.run("smarthome-secrets-migrate", "mail-rss-service", check=0)
        self.assertNotIn("IMAP_PASS", self.read(ml))
        with open(os.path.join(self.e.rundir, "mail-rss-service", "IMAP_PASS")) as fh:
            self.assertEqual(fh.read(), DUMMY["IMAP_PASS"])
        rc, out = self.e.run("smarthome-secret", "list", check=0)
        self.assertEqual(out.split("\n")[0], "beszel-agent BESZEL_TOKEN")
        for svc in ("beszel-agent", "homarr", "mail-rss-service"):
            self.e.run("smarthome-secrets-rollback", svc, check=0)
        self.assertEqual(self.read(bz), f"BESZEL_KEY=ssh-ed25519 PUB\nBESZEL_TOKEN={DUMMY['BESZEL_TOKEN']}\n")

    def test_old_compose_without_reset_is_caught(self):
        # Simulate a compose that ignores `!reset`: the rendered env still holds the value.
        fake = os.path.join(self.e.tmp, "fake-compose")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nif [ \"$2\" = --format ]; then echo '{\"services\":{\"searxng\":"
                     "{\"environment\":{\"SEARXNG_SECRET\":\"x\"},\"secrets\":[{}]}}}'; fi\nexit 0\n")
        os.chmod(fake, 0o755)
        with open(os.path.join(self.e.etc, "config"), "a") as fh:
            fh.write(f"COMPOSE_CMD={fake}\n")
        rc, out = self.e.run("smarthome-secrets-migrate", "searxng", check=1)
        self.assertIn("still rendered with a value", out)
        self.assertEqual(self.read(), self.original)  # restored


def real_creds_usable():
    if not IS_ROOT or not shutil.which("systemd-creds"):
        return False
    # Never point systemd-creds at /dev/null: as root it writes atomically (rename over the target).
    d = tempfile.mkdtemp(prefix="sms-probe-")
    try:
        p = subprocess.run(["systemd-creds", "encrypt", "--with-key=host", "--name=t", "-", os.path.join(d, "t.cred")],
                           input=b"x", capture_output=True)
        return p.returncode == 0
    finally:
        shutil.rmtree(d)


@unittest.skipUnless(real_creds_usable(), "real systemd-creds needs root + a host key")
class TestRealSystemdCreds(unittest.TestCase):
    def test_roundtrip(self):
        e = Env(creds="systemd-creds")
        try:
            e.run("smarthome-secret", "set", "searxng", "SEARXNG_SECRET", stdin=b"DUMMY-real-creds-5\n", check=0)
            cred = os.path.join(e.credstore, "searxng.SEARXNG_SECRET")
            self.assertNotIn(b"DUMMY-real-creds-5", read_bytes(cred))
            # bound to its name: decrypting under another name fails
            p = subprocess.run(["systemd-creds", "decrypt", "--name=homarr.X", cred, "-"], capture_output=True)
            self.assertNotEqual(p.returncode, 0)
            with open(os.path.join(e.etc, "owners.conf"), "w") as fh:
                fh.write("searxng 1000:1000\n")
            e.run("smarthome-secrets", "apply", check=0)
            p = os.path.join(e.rundir, "searxng", "SEARXNG_SECRET")
            st = os.stat(p)
            self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid), (0o400, 1000, 1000))
            with open(p, "rb") as fh:
                self.assertEqual(fh.read(), b"DUMMY-real-creds-5")
        finally:
            e.cleanup()


class TestInstallHost(unittest.TestCase):
    def test_destdir_install_and_dispatch(self):
        d = tempfile.mkdtemp(prefix="sms-dest-")
        try:
            p = subprocess.run(["bash", os.path.join(SECRETS, "host", "install-host.sh"), "--repo=/home/ben/smarthome"],
                               env=dict(os.environ, DESTDIR=d), capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            for n in ("smarthome-secret", "smarthome-secrets", "smarthome-secrets-migrate", "smarthome-secrets-rollback"):
                link = os.path.join(d, "usr/local/sbin", n)
                self.assertTrue(os.path.islink(link))
                q = subprocess.run([link, "--help"], capture_output=True, text=True,
                                   env=dict(os.environ, SMARTHOME_SECRETS_TEST="1"))
                self.assertIn("usage", q.stdout + q.stderr)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "etc/credstore.encrypted/smarthome")).st_mode), 0o700)
            with open(os.path.join(d, "etc/smarthome-secrets/config")) as fh:
                self.assertIn("REPO_ROOT=/home/ben/smarthome", fh.read())
            self.assertTrue(os.path.isfile(os.path.join(d, "etc/systemd/system/smarthome-secrets.service")))
            self.assertIn("not enabled", p.stdout)
        finally:
            shutil.rmtree(d)


@unittest.skipUnless(IS_ROOT and shutil.which("systemd-analyze"), "systemd-analyze verify needs root")
class TestUnitFile(unittest.TestCase):
    def test_systemd_analyze_verify(self):
        d = tempfile.mkdtemp(prefix="sms-unit-")
        try:
            os.makedirs(os.path.join(d, "etc/systemd/system"))
            os.makedirs(os.path.join(d, "usr/local/sbin"))
            shutil.copy(os.path.join(SECRETS, "systemd", "smarthome-secrets.service"), os.path.join(d, "etc/systemd/system"))
            exe = os.path.join(d, "usr/local/sbin/smarthome-secrets")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            p = subprocess.run(["systemd-analyze", "verify", f"--root={d}", "smarthome-secrets.service"],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertNotIn("smarthome-secrets.service:", p.stderr)  # no unit-file warnings
        finally:
            shutil.rmtree(d)


class TestZZNoLeaks(unittest.TestCase):
    def test_no_value_in_any_output(self):
        joined = "\n".join(ALL_OUTPUT)
        self.assertTrue(ALL_OUTPUT)
        for s in LEAK_STRINGS:
            self.assertNotIn(s, joined)


if __name__ == "__main__":
    unittest.main()
