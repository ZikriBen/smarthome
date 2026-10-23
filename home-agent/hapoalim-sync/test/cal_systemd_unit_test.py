#!/usr/bin/env python3
"""Box-only checks for cal-run.service / cal-run.path / cal-setup / setup-cal-host.sh (no systemd changes)."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
UNIT = HERE / "host" / "cal-run.service"
HAP_UNIT = HERE / "host" / "hapoalim-run.service"
SETUP = HERE / "host" / "setup-cal-host.sh"
CREDS = HERE / "host" / "cal-setup"


def fields(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith(("#", "[")) and "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


class CalUnitTests(unittest.TestCase):
    def test_same_hardening_as_hapoalim(self):
        cal, hap = fields(UNIT.read_text()), fields(HAP_UNIT.read_text())
        for k in ("Type", "UMask", "PrivateTmp", "ProtectHome", "NoNewPrivileges"):
            self.assertEqual(cal.get(k), hap.get(k), k)
        self.assertEqual(cal["ExecStart"], ["/usr/local/lib/hapoalim/cal-runner.py"])
        self.assertEqual(sorted(cal["LoadCredentialEncrypted"]), [
            "cal.password:/etc/credstore.encrypted/cal.password",
            "cal.username:/etc/credstore.encrypted/cal.username"])
        rwp = " ".join(cal["ReadWritePaths"])
        self.assertIn("/home/ben/smarthome/data/cal", rwp)
        self.assertIn("/home/ben/smarthome/home-agent/hermes/otp", rwp)
        self.assertNotIn("data/hapoalim", rwp)
        env = dict(e.split("=", 1) for e in cal["Environment"])
        self.assertEqual(env["CAL_MAX_RUNS_PER_DAY"], "2")
        self.assertEqual(env["CAL_ARCHIVE_DIR"], "/home/ben/smarthome/data/cal/archive")
        self.assertTrue(env["CAL_OUT_DIR"].startswith("/home/ben/smarthome/data/cal/"))
        self.assertEqual(cal["EnvironmentFile"], ["/etc/cal/image.env"])
        self.assertIn("limits.conf", UNIT.read_text())

    def test_path_unit(self):
        self.assertIn("PathExists=/var/lib/cal-spool/request.json", (HERE / "host" / "cal-run.path").read_text())

    def test_setup_scripts(self):
        s = SETUP.read_text()
        for needle in ("cal-run.service.d/paths.conf", "ReadWritePaths=$DATA_DIR $INBOX_DIR", "cal_archive.py",
                       "cal-runner.py", "calWalkCardData", "CAL_SYNC_IMAGE", "/var/lib/cal-spool"):
            self.assertIn(needle, s)
        self.assertNotIn("HAPOALIM_SYNC_IMAGE=", s)        # never re-pins the Hapoalim image
        c = CREDS.read_text()
        self.assertIn("read -r -s", c)
        self.assertIn("systemd-creds encrypt", c)
        self.assertNotRegex(c, r"\becho\s+\"?\$value")
        for p in (SETUP, CREDS):
            subprocess.run(["bash", "-n", str(p)], check=True)

    def test_systemd_analyze_verify(self):
        if not shutil.which("systemd-analyze"):
            self.skipTest("no systemd-analyze")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cal-run-test.service"
            body = UNIT.read_text()
            for pat in (r"^LoadCredentialEncrypted=.*\n", r"^EnvironmentFile=.*\n", r"^Requires=docker\.service\n",
                        r"^After=docker\.service\n"):
                body = re.sub(pat, "", body, flags=re.M)
            body = re.sub(r"^ExecStart=.*\n", "ExecStart=/bin/true\n", body, flags=re.M)
            p.write_text(body)
            proc = subprocess.run(["systemd-analyze", "verify", str(p)], capture_output=True, text=True)
            if proc.returncode != 0:
                err = proc.stderr + proc.stdout
                if "Permission denied" in err or "/run/systemd" in err:
                    self.skipTest("systemd-analyze needs /run/systemd")
                self.fail(err)


if __name__ == "__main__":
    unittest.main()
