#!/usr/bin/env python3
"""Optional: run the shim / native *_FILE loaders inside the REAL image filesystems.

No docker daemon needed: the image rootfs is unpacked (skopeo copy + tar) and the
entrypoint logic runs under `chroot` as root. Skipped unless the rootfs dirs are given:

  sudo SMARTHOME_SECRETS_ROOTFS_SEARXNG=/tmp/img/sxr SMARTHOME_SECRETS_ROOTFS_HOMARR=/tmp/img/hmr \\
       python3 secrets/test/test_image_integration.py -v
Prints only MATCH / NO-MATCH, never a value.
"""
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(os.path.dirname(HERE), "bin", "secrets-env-shim")
SX = os.environ.get("SMARTHOME_SECRETS_ROOTFS_SEARXNG")
HM = os.environ.get("SMARTHOME_SECRETS_ROOTFS_HOMARR")
DUMMY = "DUMMY-integration-c0ffee"


def chroot(root, env, argv):
    return subprocess.run(["chroot", root, "/usr/bin/env", "-i"] + [f"{k}={v}" for k, v in env.items()] + argv,
                          capture_output=True, text=True, timeout=120)


class _Root:
    def __init__(self, root):
        self.root = root
        self.created = []

    def put(self, rel, data, mode):
        p = os.path.join(self.root, rel.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(data)
        os.chmod(p, mode)
        self.created.append(p)

    def cleanup(self):
        for p in self.created:
            os.unlink(p)


@unittest.skipUnless(os.geteuid() == 0 and SX and os.path.isdir(SX or ""), "needs root + searxng rootfs")
class TestSearxngImage(unittest.TestCase):
    def test_shim_then_searxng_settings_reads_secret(self):
        r = _Root(SX)
        try:
            with open(SHIM) as fh:
                r.put("/usr/local/bin/secrets-env-shim", fh.read(), 0o755)
            r.put("/run/secrets/SEARXNG_SECRET", DUMMY + "\n", 0o400)
            py = ("import os, sys; sys.path.insert(0, '/usr/local/searxng'); "
                  "from searx import settings; "
                  f"ok = settings['server']['secret_key'] == {DUMMY!r} and 'SEARXNG_SECRET_FILE' not in os.environ; "
                  "print('MATCH' if ok else 'NO-MATCH')")
            p = chroot(SX, {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                            "SEARXNG_SECRET_FILE": "/run/secrets/SEARXNG_SECRET",
                            "SECRETS_FROM_FILES": "SEARXNG_SECRET",
                            "SEARXNG_SETTINGS_PATH": "/usr/local/searxng/searx/settings.yml"},
                       ["/usr/local/bin/secrets-env-shim", "/usr/local/searxng/.venv/bin/python", "-c", py])
            self.assertNotIn(DUMMY, p.stdout + p.stderr)
            self.assertEqual(p.stdout.strip().splitlines()[-1], "MATCH", p.stderr[-800:])
            # busybox sh from the image itself runs the shim
            p = chroot(SX, {"PATH": "/usr/bin:/bin", "SEARXNG_SECRET_FILE": "/run/secrets/SEARXNG_SECRET",
                            "SECRETS_FROM_FILES": "SEARXNG_SECRET"},
                       ["/bin/sh", "/usr/local/bin/secrets-env-shim", "/bin/sh", "-c",
                        f'[ "$SEARXNG_SECRET" = "{DUMMY}" ] && echo MATCH || echo NO-MATCH'])
            self.assertEqual(p.stdout.strip(), "MATCH", p.stderr)
        finally:
            r.cleanup()


@unittest.skipUnless(os.geteuid() == 0 and HM and os.path.isdir(HM or ""), "needs root + homarr rootfs")
class TestHomarrImage(unittest.TestCase):
    def test_native_file_support(self):
        r = _Root(HM)
        try:
            r.put("/run/secrets/SECRET_ENCRYPTION_KEY", DUMMY + "\n", 0o400)
            p = chroot(HM, {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                            "SECRET_ENCRYPTION_KEY_FILE": "/run/secrets/SECRET_ENCRYPTION_KEY"},
                       ["/app/entrypoint.sh", "sh", "-c",
                        f'[ "$SECRET_ENCRYPTION_KEY" = "{DUMMY}" ] && echo MATCH || echo NO-MATCH'])
            self.assertNotIn(DUMMY, p.stdout + p.stderr)
            self.assertEqual(p.stdout.strip().splitlines()[-1], "MATCH", p.stdout + p.stderr)
            self.assertIn("Loaded secret for SECRET_ENCRYPTION_KEY", p.stdout)
        finally:
            r.cleanup()


if __name__ == "__main__":
    unittest.main()
