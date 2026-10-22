#!/usr/bin/env python3
"""Root host runner for ONE Bank Hapoalim sync (hapoalim-run.service, started by hapoalim-run.path when the
Command Center drops spool/request.json because Ben asked Hermes). Installed root-owned to /usr/local/lib/hapoalim/
by setup-host.sh, so an edit in the repo checkout does not change what root runs.

Order (nothing reaches the bank before step 6):
 1. delete CSVs older than HAPOALIM_RETENTION_DAYS (default 7) in HAPOALIM_OUT_DIR — every run, even refused ones;
 2. one run at a time: flock on $STATE/run.lock (non-blocking) -> LOCKED;
 3. take the request (request.json -> running.json): run_id + requested_at (the moment Ben asked);
 4. refuse if $STATE/stop-<YYYY-MM-DD> exists (Asia/Jerusalem day; written after any auth/OTP failure) -> STOPPED_TODAY,
    or if HAPOALIM_MAX_RUNS_PER_DAY runs already started today -> DAILY_LIMIT;
 5. credentials from $CREDENTIALS_DIRECTORY (systemd LoadCredentialEncrypted=, sealed by hapoalim-setup) -> NOT_CONFIGURED;
 6. count the run, start the hardened one-shot container (no mounts, no env secrets), write the credentials to its
    stdin; when it says otp_needed, wait <= HAPOALIM_OTP_TIMEOUT (120 s) for ONE hapoalim code that arrived after
    requested_at in the shared OTP inbox (otp_take.py, as the inbox owner; read once, then deleted) and pass it on stdin;
 7. CSV (0600, HAPOALIM_OUT_UID:GID) into HAPOALIM_OUT_DIR; allowlisted status (no amounts) into the spool;
    stop-for-today file after any STOP status. Container stdout is parsed, never logged; stderr is discarded.
"""
import fcntl
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")
HERE = Path(__file__).resolve().parent
RUN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
CSV_RE = re.compile(r"^hapoalim-\d{8}-\d{6}(?:-[a-z0-9_]+)*(?:-[0-9a-f]{4})?\.csv$")
STOP_STATUSES = {"INVALID_PASSWORD", "CHANGE_PASSWORD", "ACCOUNT_BLOCKED", "LOGIN_UNKNOWN", "OTP_TIMEOUT",
                 "OTP_REJECTED", "OTP_NO_RESULT", "OTP_FORM_CHANGED", "OTP_REQUIRED", "OTP_AMBIGUOUS",
                 "CAPTCHA_OR_BLOCK", "ACCOUNTS_FAILED", "TRANSACTIONS_FAILED", "INTERNAL_AFTER_LOGIN",
                 "RUN_TIMEOUT", "PROTOCOL_ERROR", "INTERNAL"}
# Only these report fields leave the runner (status files read by the Command Center / Hermes). No amounts,
# balances, descriptions or full account numbers ever.
REPORT_FIELDS = ("status", "stage", "stopForToday", "otpRequested", "otpDelivered", "blockedHosts", "failedHosts",
                 "seconds", "accountCount", "transactionCount", "firstDate", "lastDate", "monthsCovered",
                 "from", "to", "profileWiped", "fakeBank", "warnings")
REQUEST_MAX_AGE = 15 * 60


def env_int(env, name, default, lo, hi):
    value = str(env.get(name, "")).strip()
    return int(value) if value.isdigit() and lo <= int(value) <= hi else default


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.image = env.get("HAPOALIM_SYNC_IMAGE", "").strip()
        self.out_dir = Path(env.get("HAPOALIM_OUT_DIR", "/home/ben/smarthome/data/hapoalim/out"))
        self.out_uid = env_int(env, "HAPOALIM_OUT_UID", 1000, 0, 2**31)
        self.out_gid = env_int(env, "HAPOALIM_OUT_GID", 1000, 0, 2**31)
        self.retention_days = env_int(env, "HAPOALIM_RETENTION_DAYS", 7, 1, 365)
        self.inbox_db = Path(env.get("HAPOALIM_INBOX_DB", "/home/ben/smarthome/home-agent/hermes/otp/codes.sqlite"))
        self.state_dir = Path(env.get("HAPOALIM_STATE_DIR", "/var/lib/hapoalim"))
        self.spool_dir = Path(env.get("HAPOALIM_SPOOL_DIR", "/var/lib/hapoalim-spool"))
        self.otp_timeout = env_int(env, "HAPOALIM_OTP_TIMEOUT", 120, 1, 290)
        self.days = env_int(env, "HAPOALIM_DAYS", 45, 1, 365)
        self.max_runs = env_int(env, "HAPOALIM_MAX_RUNS_PER_DAY", 3, 1, 10)
        self.deadline = env_int(env, "HAPOALIM_RUN_DEADLINE", 420, 60, 1800)
        self.container_uid = env_int(env, "HAPOALIM_CONTAINER_UID", 65534, 1, 2**31)
        self.container_gid = env_int(env, "HAPOALIM_CONTAINER_GID", 65534, 1, 2**31)
        self.mask_accounts = "0" if env.get("HAPOALIM_MASK_ACCOUNTS", "1").strip() == "0" else "1"
        self.extra_hosts = re.sub(r"[^a-z0-9., -]", "", env.get("HAPOALIM_EXTRA_HOSTS", "").lower())[:200]
        self.creds_dir = env.get("CREDENTIALS_DIRECTORY", "")
        # Tests only: run a local command instead of docker. Refused for root, so a deployed unit can never use it.
        self.test_exec = None
        if env.get("HAPOALIM_RUNNER_TEST_EXEC") and os.geteuid() != 0:
            self.test_exec = json.loads(env["HAPOALIM_RUNNER_TEST_EXEC"])


def today(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, TZ).strftime("%Y-%m-%d")


def atomic_write(path, data, mode=0o640, uid=None, gid=None):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        if uid is not None and os.geteuid() == 0:
            os.chown(tmp, uid, gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cleanup_old_csv(out_dir, retention_days, now=None):
    """Delete hapoalim-*.csv older than retention_days. Returns the number deleted. Never follows symlinks."""
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(out_dir))
    except OSError:
        return 0
    for entry in entries:
        if not CSV_RE.match(entry.name) or not entry.is_file(follow_symlinks=False):
            continue
        try:
            if now - entry.stat(follow_symlinks=False).st_mtime > retention_days * 86400:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            pass
    return removed


class Runner:
    def __init__(self, cfg, clock=time.time):
        self.cfg = cfg
        self.clock = clock
        self.t0 = clock()

    # ------------------------------------------------------------------ state
    def stop_file(self, now=None):
        return self.cfg.state_dir / f"stop-{today(now)}"

    def runs_file(self, now=None):
        return self.cfg.state_dir / f"runs-{today(now)}"

    def runs_today(self):
        try:
            return int(self.runs_file().read_text().strip() or "0")
        except (OSError, ValueError):
            return 0

    def write_stop(self, status):
        atomic_write(self.stop_file(), json.dumps({"status": status, "at": self.clock()}) + "\n", 0o600)

    def prune_state(self):
        keep = {self.stop_file().name, self.runs_file().name}
        for path in list(self.cfg.state_dir.glob("stop-*")) + list(self.cfg.state_dir.glob("runs-*")):
            if path.name not in keep:
                try:
                    path.unlink()
                except OSError:
                    pass
        for path in self.cfg.spool_dir.glob("status-*.json"):
            try:
                if self.clock() - path.stat().st_mtime > 2 * 86400:
                    path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ request
    def take_request(self, claim=True):
        """claim=True: request.json -> running.json (ours). claim=False (another run holds the lock): read and drop
        request.json without touching that run's running.json."""
        req, running = self.cfg.spool_dir / "request.json", self.cfg.spool_dir / "running.json"
        if not claim:
            running = self.cfg.spool_dir / f".refused-{os.getpid()}.json"
        try:
            os.replace(req, running)
        except FileNotFoundError:
            return None
        try:
            data = json.loads(running.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        run_id = str(data.get("run_id", "")) if isinstance(data, dict) else ""
        try:
            requested_at = float(data.get("requested_at"))
        except (TypeError, ValueError, AttributeError):
            requested_at = 0.0
        actions = data.get("actions") if isinstance(data, dict) and isinstance(data.get("actions"), list) else None
        if not claim:
            try:
                running.unlink()
            except OSError:
                pass
        return {"run_id": run_id if RUN_ID_RE.fullmatch(run_id) else None, "requested_at": requested_at,
                "actions": actions}

    def publish(self, run_id, status, last=True):
        body = json.dumps(status, ensure_ascii=False) + "\n"
        if run_id:
            atomic_write(self.cfg.spool_dir / f"status-{run_id}.json", body, 0o640)
        if last:
            atomic_write(self.cfg.spool_dir / "last-status.json", body, 0o640)

    # ------------------------------------------------------------------ container
    def container_argv(self, run_id):
        if self.cfg.test_exec:
            return list(self.cfg.test_exec)
        uid, gid = self.cfg.container_uid, self.cfg.container_gid
        argv = ["docker", "run", "--rm", "-i", "--name", f"hapoalim-sync-{run_id}",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "512", "--memory", "2g", "--shm-size", "256m",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
                "--tmpfs", f"/profile:rw,nosuid,nodev,size=256m,mode=0700,uid={uid},gid={gid}",
                "--user", f"{uid}:{gid}", "--log-driver", "none",
                "-e", f"HAPOALIM_DAYS={self.cfg.days}",
                "-e", f"HAPOALIM_MASK_ACCOUNTS={self.cfg.mask_accounts}",
                "-e", f"HAPOALIM_OTP_WAIT_MS={(self.cfg.otp_timeout + 30) * 1000}"]
        if self.cfg.extra_hosts:
            argv += ["-e", f"HAPOALIM_EXTRA_HOSTS={self.cfg.extra_hosts}"]
        return argv + [self.cfg.image]

    def read_credentials(self):
        if not self.cfg.creds_dir:
            return None
        try:
            user = (Path(self.cfg.creds_dir) / "hapoalim.user_code").read_text(encoding="utf-8").strip()
            password = (Path(self.cfg.creds_dir) / "hapoalim.password").read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            return None
        return (user, password) if user and password else None

    def wait_for_code(self, since):
        """One hapoalim code from the shared inbox, as the inbox owner. {"code"} | {"error"}."""
        argv = [sys.executable, str(HERE / "otp_take.py"), str(self.cfg.inbox_db), repr(since), str(self.cfg.otp_timeout)]
        kwargs = {}
        if os.geteuid() == 0:
            try:
                st = self.cfg.inbox_db.parent.stat()
            except OSError:
                return {"error": "OTP_TIMEOUT"}
            if st.st_uid == 0:
                return {"error": "OTP_TIMEOUT"}  # never read a root-owned inbox as root: misconfigured
            kwargs = {"user": st.st_uid, "group": st.st_gid, "extra_groups": []}
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=self.cfg.otp_timeout + 20,
                                 env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, **kwargs).stdout
            reply = json.loads(out.strip().splitlines()[-1])
        except Exception:
            return {"error": "OTP_TIMEOUT"}
        if isinstance(reply.get("code"), str) and re.fullmatch(r"[0-9]{4,8}", reply["code"]):
            return {"code": reply["code"]}
        return {"error": "OTP_AMBIGUOUS" if reply.get("error") == "OTP_AMBIGUOUS" else "OTP_TIMEOUT"}

    def run_container(self, run_id, creds, since, actions=None):
        """-> (report dict, files list, otp_wait str or None)."""
        proc = subprocess.Popen(self.container_argv(run_id), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        lines = queue.Queue()

        def pump():
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        otp_wait = None
        payload = {"userCode": creds[0], "password": creds[1]}
        if actions:
            payload["actions"] = actions
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except OSError:
            pass
        creds = payload = None
        deadline = self.t0 + self.cfg.deadline
        report, files = None, []
        try:
            while True:
                left = deadline - self.clock()
                if left <= 0:
                    report = {"status": "RUN_TIMEOUT", "stage": "unknown"}
                    break
                try:
                    line = lines.get(timeout=min(left, 5))
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # never echo a non-protocol line
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "otp_needed" and otp_wait is None:
                    got = self.wait_for_code(since)
                    otp_wait = "delivered" if "code" in got else got["error"]
                    reply = {"otp": got["code"]} if "code" in got else {"otp": None, "error": got["error"]}
                    del got
                    try:
                        proc.stdin.write(json.dumps(reply) + "\n")
                        proc.stdin.flush()
                    except OSError:
                        pass
                    del reply
                elif event.get("event") == "result":
                    report = event.get("report") if isinstance(event.get("report"), dict) else None
                    raw = event.get("files")
                    if isinstance(raw, list):
                        files = [f for f in raw if isinstance(f, dict) and isinstance(f.get("body"), str)]
                    elif isinstance(event.get("csv"), str):  # legacy single-csv
                        files = [{"kind": "csv", "nameHint": "checking_transactions", "body": event["csv"]}]
                    break
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                if not self.cfg.test_exec:
                    subprocess.run(["docker", "kill", f"hapoalim-sync-{run_id}"], capture_output=True, timeout=30)
                proc.kill()
                proc.wait(timeout=10)
        if report is None:
            report = {"status": "PROTOCOL_ERROR", "stage": "unknown"}
        return report, files, otp_wait

    def write_files(self, files):
        """Write action output files (CSVs) to out_dir. Returns [{name, path, rows, kind, nameHint}]."""
        out = self.cfg.out_dir
        if not out.is_dir():
            os.makedirs(out, mode=0o700, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(out, self.cfg.out_uid, self.cfg.out_gid)
        stamp = datetime.fromtimestamp(self.clock(), TZ).strftime("%Y%m%d-%H%M%S")
        written = []
        for i, f in enumerate(files):
            kind = f.get("kind") or "csv"
            hint = re.sub(r"[^a-z0-9_]+", "", str(f.get("nameHint") or kind).lower())[:40] or kind
            name = f"hapoalim-{stamp}-{hint}.csv" if len(files) == 1 else f"hapoalim-{stamp}-{hint}-{i+1}.csv"
            if not CSV_RE.match(name.replace(hint, "x") if False else name) and not name.endswith(".csv"):
                name = f"hapoalim-{stamp}-{i+1}.csv"
            # Accept our own naming even when hint has underscores (CSV_RE is for retention cleanup).
            if (out / name).exists():
                name = f"hapoalim-{stamp}-{hint}-{os.urandom(2).hex()}.csv"
            atomic_write(out / name, f["body"], 0o600, self.cfg.out_uid, self.cfg.out_gid)
            written.append({"name": name, "path": str(out / name), "kind": kind, "nameHint": hint,
                            "rows": max(f["body"].count("\n") - 1, 0)})
        return written

    # ------------------------------------------------------------------ main
    def run(self):
        cfg = self.cfg
        os.umask(0o077)
        cfg.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        removed = cleanup_old_csv(cfg.out_dir, cfg.retention_days, self.clock())
        lock = open(cfg.state_dir / "run.lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            request = self.take_request(claim=False)
            lock.close()
            if request is None:
                return None, 3
            return self.finish(request, {"status": "LOCKED", "stage": "preflight"}, publish_last=False), 3
        try:
            self.prune_state()
            request = self.take_request()
            if request is None:
                return None, 0  # the path unit fired without a request (or a duplicate trigger)
            status, code = self._run_locked(request, removed)
            return status, code
        finally:
            try:
                (cfg.spool_dir / "running.json").unlink()
            except OSError:
                pass
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def _run_locked(self, request, removed):
        cfg = self.cfg
        now = self.clock()
        if not request["run_id"] or not (now - REQUEST_MAX_AGE <= request["requested_at"] <= now + 5):
            return self.finish(request, {"status": "BAD_REQUEST", "stage": "preflight"}), 2
        if self.stop_file().exists():
            return self.finish(request, {"status": "STOPPED_TODAY", "stage": "preflight"}), 2
        if self.runs_today() >= cfg.max_runs:
            return self.finish(request, {"status": "DAILY_LIMIT", "stage": "preflight"}), 2
        if not cfg.image and not cfg.test_exec:
            return self.finish(request, {"status": "NOT_CONFIGURED", "stage": "preflight"}), 2
        creds = self.read_credentials()
        if creds is None:
            return self.finish(request, {"status": "NOT_CONFIGURED", "stage": "preflight"}), 2
        atomic_write(self.runs_file(), f"{self.runs_today() + 1}\n", 0o600)   # counted BEFORE the bank
        since = max(request["requested_at"], now - REQUEST_MAX_AGE)
        report, files, otp_wait = self.run_container(request["run_id"], creds, since, request.get("actions"))
        creds = None
        extra = {"otpWait": otp_wait, "csvRemovedByRetention": removed}
        if report.get("status") == "DONE" and files:
            try:
                written = self.write_files(files)
                extra["files"] = [{"name": w["name"], "path": w["path"], "rows": w["rows"],
                                   "kind": w["kind"], "nameHint": w["nameHint"]} for w in written]
                # Convenience aliases for the common single-CSV case (Hermes/tooling).
                if written:
                    extra.update(csv=written[0]["name"], csvPath=written[0]["path"], csvRows=written[0]["rows"])
            except OSError:
                report = dict(report, status="CSV_WRITE_FAILED")
        files = None
        # Drop action file bodies / free-text details from what we publish.
        if isinstance(report.get("actions"), list):
            report = dict(report, actions=[{k: a[k] for k in ("name", "ok", "status", "warnings", "summary")
                                            if k in a} for a in report["actions"] if isinstance(a, dict)])
            extra["actions"] = report["actions"]
        status = self.finish(request, report, extra)
        return status, 0 if status["status"] == "DONE" else 1

    def finish(self, request, report, extra=None, publish_last=True):
        status = {k: report[k] for k in REPORT_FIELDS if k in report}
        status["status"] = str(status.get("status") or "INTERNAL")
        if status["status"] in STOP_STATUSES or report.get("stopForToday") is True:
            status["stopForToday"] = True
            try:
                self.write_stop(status["status"])
            except OSError:
                pass
        status.update(extra or {})
        status.update(runId=(request or {}).get("run_id"), finishedAt=self.clock(),
                      requestedAt=(request or {}).get("requested_at"))
        try:
            self.publish((request or {}).get("run_id"), status, publish_last)
        except OSError:
            pass
        return status


def main():
    status, code = Runner(Config()).run()
    if status is not None:
        # journald gets one line: status only (no amounts, no codes, no free text from the bank).
        print(f"hapoalim-sync: {status['status']} run={status.get('runId')} rows={status.get('csvRows', 0)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
