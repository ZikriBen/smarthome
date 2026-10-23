#!/usr/bin/env python3
"""Root host runner for ONE Cal (visaCal) card sync (cal-run.service, started by cal-run.path when the Command
Center drops /var/lib/cal-spool/request.json after Ben approved `card_sync`). Installed root-owned to
/usr/local/lib/hapoalim/ next to hapoalim-runner.py by setup-cal-host.sh.

Same order and guarantees as hapoalim-runner.py (it subclasses that Runner; only Cal specifics differ):
 retention cleanup of cal-*.csv -> flock -> take request -> STOPPED_TODAY / COVERED (no login) / DAILY_LIMIT
 (CAL_MAX_RUNS_PER_DAY, default 2) -> NOT_CONFIGURED (cal.username / cal.password via LoadCredentialEncrypted,
 sealed by cal-setup) -> count the run -> hardened one-shot container (`node /app/cal-auto.mjs`, no mounts, no
 env secrets) -> ONE code from the OTP inbox site "cal" only if Cal shows a code form -> CSV into CAL_OUT_DIR,
 archive merge into CAL_ARCHIVE_DIR (cal_archive.py) -> status into the spool; stop-for-today after any auth/OTP
 failure or a 429. Container output is parsed, never logged.
"""
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
_spec = importlib.util.spec_from_file_location("hapoalim_runner", HERE / "hapoalim-runner.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
import cal_archive  # noqa: E402

env_int = base.env_int
CAL_CSV_RE = re.compile(r"^cal-\d{8}-\d{6}(?:-[a-z0-9_]+)*(?:-[0-9a-f]{4})?\.csv$")
CAL_STOP = {"INVALID_PASSWORD", "CHANGE_PASSWORD", "ACCOUNT_BLOCKED", "LOGIN_UNKNOWN", "OTP_TIMEOUT",
            "OTP_REJECTED", "OTP_NO_RESULT", "OTP_FORM_CHANGED", "OTP_REQUIRED", "OTP_AMBIGUOUS",
            "CAPTCHA_OR_BLOCK", "CAL_BLOCKED", "CARDS_FAILED", "NO_CARDS", "ACTION_ERROR", "RUN_TIMEOUT",
            "PROTOCOL_ERROR", "INTERNAL"}
# Allowlisted status fields (no amounts, descriptions or card numbers beyond last-4).
CAL_FIELDS = ("site", "status", "stage", "stopForToday", "blocked", "otpRequested", "otpDelivered", "blockedHosts",
              "failedHosts", "apiHosts", "blockedApiHosts", "seconds", "cardCount", "transactionCount",
              "pendingCount", "monthsCovered", "from_month", "to_month", "cards", "profileWiped", "fakeCal",
              "warnings", "runsToday", "maxRuns", "coverage",
              "archiveNew", "archiveRemoved", "archiveMonths", "archiveCompleteMonths", "archivePendingRows",
              "archiveWarnings", "archiveSkipped")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class CalConfig:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.image = (env.get("CAL_SYNC_IMAGE", "") or env.get("HAPOALIM_SYNC_IMAGE", "")).strip()
        self.out_dir = Path(env.get("CAL_OUT_DIR", "/home/ben/smarthome/data/cal/out"))
        self.out_uid = env_int(env, "CAL_OUT_UID", 1000, 0, 2**31)
        self.out_gid = env_int(env, "CAL_OUT_GID", 1000, 0, 2**31)
        self.retention_days = env_int(env, "CAL_RETENTION_DAYS", 7, 1, 365)
        self.inbox_db = Path(env.get("CAL_INBOX_DB", "/home/ben/smarthome/home-agent/hermes/otp/codes.sqlite"))
        self.state_dir = Path(env.get("CAL_STATE_DIR", "/var/lib/cal"))
        self.spool_dir = Path(env.get("CAL_SPOOL_DIR", "/var/lib/cal-spool"))
        self.otp_timeout = env_int(env, "CAL_OTP_TIMEOUT", 120, 1, 290)
        self.months = env_int(env, "CAL_MONTHS", 3, 1, 19)
        self.max_runs = env_int(env, "CAL_MAX_RUNS_PER_DAY", 2, 1, 10)
        self.deadline = env_int(env, "CAL_RUN_DEADLINE", 600, 60, 1800)
        self.container_uid = env_int(env, "CAL_CONTAINER_UID", 65534, 1, 2**31)
        self.container_gid = env_int(env, "CAL_CONTAINER_GID", 65534, 1, 2**31)
        self.extra_hosts = re.sub(r"[^a-z0-9., -]", "", env.get("CAL_EXTRA_HOSTS", "").lower())[:200]
        self.otp_entry = "0" if env.get("CAL_OTP_ENTRY", "1").strip() == "0" else "1"
        self.month_cap = env_int(env, "CAL_MONTH_CAP", 1000, 2, 100000)
        self.walk_delay_ms = env_int(env, "CAL_WALK_DELAY_MS", 300, 0, 10000)
        # "page" (default: Cal API calls from inside the browser page, port of upstream PR #1193) | "node" (6.9.0).
        self.api_transport = "node" if env.get("CAL_API_TRANSPORT", "").strip() == "node" else "page"
        self.creds_dir = env.get("CREDENTIALS_DIRECTORY", "")
        self.archive_dir = Path(env.get("CAL_ARCHIVE_DIR", "/home/ben/smarthome/data/cal/archive"))
        self.repull_days = env_int(env, "CAL_REPULL_DAYS", cal_archive.DEFAULT_REPULL_DAYS, 0, 40)
        self.days = 0
        self.mask_accounts = "1"
        self.test_exec = None
        if env.get("CAL_RUNNER_TEST_EXEC") and os.geteuid() != 0:
            self.test_exec = json.loads(env["CAL_RUNNER_TEST_EXEC"])


class CalRunner(base.Runner):
    NAME = "cal"
    CSV_PATTERN = CAL_CSV_RE
    STOP = CAL_STOP
    FIELDS = CAL_FIELDS
    OTP_SITE = "cal"

    def container_argv(self, run_id):
        if self.cfg.test_exec:
            return list(self.cfg.test_exec)
        uid, gid = self.cfg.container_uid, self.cfg.container_gid
        argv = ["docker", "run", "--rm", "-i", "--name", f"cal-sync-{run_id}",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "512", "--memory", "2g", "--shm-size", "256m",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
                "--tmpfs", f"/profile:rw,nosuid,nodev,size=256m,mode=0700,uid={uid},gid={gid}",
                "--user", f"{uid}:{gid}", "--log-driver", "none",
                "-e", f"CAL_MONTHS={self.cfg.months}",
                "-e", f"CAL_OTP_ENTRY={self.cfg.otp_entry}",
                "-e", f"CAL_MONTH_CAP={self.cfg.month_cap}",
                "-e", f"CAL_WALK_DELAY_MS={self.cfg.walk_delay_ms}",
                "-e", f"CAL_API_TRANSPORT={self.cfg.api_transport}",
                "-e", f"CAL_OTP_WAIT_MS={(self.cfg.otp_timeout + 30) * 1000}"]
        if self.cfg.extra_hosts:
            argv += ["-e", f"CAL_EXTRA_HOSTS={self.cfg.extra_hosts}"]
        return argv + ["--entrypoint", "dumb-init", self.cfg.image, "--", "node", "/app/cal-auto.mjs"]

    def read_credentials(self):
        if not self.cfg.creds_dir:
            return None
        try:
            user = (Path(self.cfg.creds_dir) / "cal.username").read_text(encoding="utf-8").strip()
            password = (Path(self.cfg.creds_dir) / "cal.password").read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            return None
        return (user, password) if user and password else None

    def credential_payload(self, creds):
        return {"username": creds[0], "password": creds[1]}

    # ------------------------------------------------------------------ months / coverage
    def month_range(self, request):
        cur = cal_archive.month_il(self.clock())
        from_m = to_m = None
        for a in request.get("actions") or []:
            if isinstance(a, dict) and a.get("name") == "card_transactions":
                from_m = a.get("from_month") or from_m
                to_m = a.get("to_month") or to_m
        to_m = str(to_m) if to_m and MONTH_RE.match(str(to_m)) else cal_archive.add_months(cur, 1)
        from_m = str(from_m) if from_m and MONTH_RE.match(str(from_m)) else cal_archive.add_months(cur, -(self.cfg.months - 1))
        return min(from_m, to_m), to_m

    def maybe_covered(self, request):
        try:
            from_m, to_m = self.month_range(request)
            cal_archive.ensure_archive_dir(self.cfg.archive_dir, uid=self.cfg.out_uid, gid=self.cfg.out_gid)
            plan = cal_archive.plan_gaps(self.cfg.archive_dir, from_m, to_m, now=self.clock())
        except Exception:
            return None
        if plan.get("covered"):
            return {"site": "cal", "status": "COVERED", "stage": "archive", "from_month": from_m, "to_month": to_m,
                    "coverage": {"needed": [], "cards": plan.get("cards")}, "cardCount": len(plan.get("cards") or [])}
        request["_fetch_from"] = plan.get("fetch_from") or from_m
        request["_fetch_to"] = plan.get("fetch_to") or to_m
        request["_gap_plan"] = {"needed": plan.get("needed"), "fetch_from": request["_fetch_from"],
                                "fetch_to": request["_fetch_to"]}
        return None

    def plan_actions(self, request):
        actions = [a for a in (request.get("actions") or []) if isinstance(a, dict)] or [{"name": "card_transactions"}]
        out = []
        for a in actions:
            a = dict(a)
            if a.get("name") == "card_transactions" and request.get("_fetch_from") and request.get("_fetch_to"):
                a["from_month"], a["to_month"] = request["_fetch_from"], request["_fetch_to"]
            out.append(a)
        return out

    def ingest_archive(self, request, files, report):
        payloads = []
        for f in files or []:
            if f.get("kind") != "archive_rows":
                continue
            try:
                p = json.loads(f["body"])
            except (ValueError, TypeError):
                continue
            if isinstance(p, dict) and isinstance(p.get("retrieval"), dict):
                payloads.append(p)
        if not payloads:
            return {"archiveSkipped": "no archive_rows payload (old image?)"}
        out = {"archiveNew": 0, "archiveRemoved": 0, "archiveMonths": [], "archiveCompleteMonths": [],
               "archivePendingRows": 0, "archiveWarnings": []}
        for p in payloads:
            res = cal_archive.merge_sync(self.cfg.archive_dir, p, run_id=str(request.get("run_id") or ""),
                                         now=self.clock(), repull_days=self.cfg.repull_days,
                                         uid=self.cfg.out_uid, gid=self.cfg.out_gid)
            out["archiveNew"] += res["new"]
            out["archiveRemoved"] += res["removed"]
            out["archiveMonths"] = sorted(set(out["archiveMonths"]) | set(res["months"]))
            out["archiveCompleteMonths"] += res["complete_months"]
            out["archivePendingRows"] += res["pending_rows"]
            out["archiveWarnings"] += res["warnings"]
        return out


def main():
    status, code = CalRunner(CalConfig()).run()
    if status is not None:
        print(f"cal-sync: {status['status']} run={status.get('runId')} rows={status.get('csvRows', 0)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
