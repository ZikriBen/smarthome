"""Bank Hapoalim monthly report: status, an approval-gated run request, and the one-code OTP broker.

The Command Center never sees the bank credentials, the browser profile or the report. It only:
  * shows a sanitized status (copied by the host runner into the spool dir; no balances/accounts/transactions);
  * proposes a run, which Ben must approve EVERY time (approvals action ``hapoalim_run_report``); the approved
    handler only drops ``request.json`` into the spool dir, and the root host runner (systemd path unit,
    hapoalim-sync/host/) starts the one-shot scraper container;
  * serves the scraper ONE relayed SMS code over a dedicated Unix socket (``broker.sock``) that is mounted
    only into the scraper container. The socket answers only for the currently approved run, only codes that
    arrived after that run's login started, at most once per run, and never anything but the ``hapoalim`` code.
    The scraper gets no access to codes.sqlite, the Command Center API, or Hermes data.

Lockout safety lives in the scraper itself (run-state.json in its own state dir); the status copied here mirrors
it so a proposal can be refused early (once a day at most; never after an authentication failure).
"""

import json
import os
import re
import socket
import socketserver
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import approvals, otp_inbox

SITE = "hapoalim"
ACTION = "hapoalim_run_report"
TZ = ZoneInfo("Asia/Jerusalem")
SPOOL_DIR = Path(os.environ.get("HAPOALIM_SPOOL_DIR", "/hapoalim-spool"))
BROKER_DIR = Path(os.environ.get("HAPOALIM_BROKER_DIR", "/hapoalim-otp"))
BROKER_SOCK = BROKER_DIR / "broker.sock"
INBOX_DB = Path(os.environ.get("OTP_INBOX_DB", "/otp-inbox/codes.sqlite"))
SCRAPER_GID = int(os.environ.get("HAPOALIM_SCRAPER_GID", "10077"))
RUN_WINDOW = 30 * 60          # an approved request must start (and ask for its code) within this window
MAX_WAIT = 180                # one code request waits at most this long
POLL = 2.0
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
# Fields of the runner's status that may leave the spool dir (everything else is dropped).
STATUS_FIELDS = ("status", "errorType", "reportMonth", "completedAt", "startedAt", "transactionCount",
                 "otpRequested", "otpUsed", "profile", "blockedHosts", "runId")
ERROR_TEXT_HE = {
    "INVALID_PASSWORD": "הבנק דחה את הסיסמה. ננעל: לא ננסה שוב עד שבן יבדוק ויאפס ידנית ב-NUC.",
    "CHANGE_PASSWORD": "הבנק מבקש להחליף סיסמה. ננעל עד טיפול ידני.",
    "ACCOUNT_BLOCKED": "הבנק חסם את החשבון לגישה. ננעל עד טיפול ידני.",
    "OTP_REJECTED": "הבנק דחה את קוד ה-SMS (ניסיון אחד בלבד). ננעל עד בדיקה ידנית.",
    "OTP_NO_RESULT": "לא הייתה תשובה מהבנק אחרי קוד ה-SMS. ננעל עד בדיקה ידנית.",
    "OTP_FORM_CHANGED": "טופס ה-SMS של הבנק השתנה (selectors לא מאומתים). ננעל עד עדכון.",
    "OTP_TIMEOUT": "קוד ה-SMS לא הגיע בזמן דרך ה-relay. אפשר לנסות מחר.",
    "OTP_AMBIGUOUS": "הגיעו שני קודים שונים, לא השתמשתי באף אחד. אפשר לנסות מחר.",
    "OTP_REQUIRED": "הבנק ביקש SMS ואין מקור קוד מוגדר.",
    "DAILY_LIMIT": "כבר הייתה ריצה היום (מקסימום אחת ביום).",
    "LOCKED_OUT": "נעול אחרי כשל הזדהות. צריך איפוס ידני ב-NUC.",
}

_lock = threading.Lock()
_server = None


# --------------------------------------------------------------------------- files

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def _today(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, TZ).strftime("%Y-%m-%d")


def _previous_month(now=None):
    day = datetime.fromtimestamp(time.time() if now is None else now, TZ)
    year, month = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    return f"{year:04d}-{month:02d}"


def _active_request(now=None):
    """The approved request (request.json, or running.json once the runner took it) if still in its window."""
    now = time.time() if now is None else now
    for name in ("running.json", "request.json"):
        data = _read_json(SPOOL_DIR / name)
        if data and RUN_ID_RE.fullmatch(str(data.get("run_id", ""))):
            approved = float(data.get("approved_at") or 0)
            if 0 < approved <= now + 5 and now - approved <= RUN_WINDOW:
                return dict(data, stage="running" if name == "running.json" else "queued")
    return None


def _state():
    """Mirror of the scraper's run-state (lastRunDay, lockout) written by the host runner. Sanitized."""
    data = _read_json(SPOOL_DIR / "state.json") or {}
    lockout = data.get("lockout") if isinstance(data.get("lockout"), dict) else None
    return {"lastRunDay": str(data.get("lastRunDay") or "") or None,
            "lockout": {"reason": str(lockout.get("reason") or "unknown"), "at": lockout.get("at")}
            if lockout else None}


# --------------------------------------------------------------------------- status / propose / execute

def status(_payload=None, _query=None):
    last = _read_json(SPOOL_DIR / "status.json") or {}
    last = {key: last[key] for key in STATUS_FIELDS if key in last}
    state = _state()
    active = _active_request()
    out = {"last_run": last or None, "last_run_day": state["lastRunDay"], "locked_out": state["lockout"],
           "pending_run": {"run_id": active["run_id"], "month": active.get("month"), "stage": active["stage"]}
           if active else None,
           "can_run_today": not state["lockout"] and state["lastRunDay"] != _today() and not active}
    if last.get("errorType") in ERROR_TEXT_HE:
        out["error_text"] = ERROR_TEXT_HE[last["errorType"]]
    return out


def _refusal(now=None):
    state = _state()
    if state["lockout"]:
        return {"status": "locked_out", "text": ERROR_TEXT_HE["LOCKED_OUT"]}
    if state["lastRunDay"] == _today(now):
        return {"status": "daily_limit", "text": ERROR_TEXT_HE["DAILY_LIMIT"]}
    if _active_request(now):
        return {"status": "already_pending", "text": "יש כבר ריצה מאושרת שממתינה או רצה."}
    return None


def propose_run(payload, _query=None):
    month = str((payload or {}).get("month") or "").strip() or _previous_month()
    if not MONTH_RE.fullmatch(month):
        return {"status": "bad_request", "text": "month must be YYYY-MM"}
    refusal = _refusal()
    if refusal:
        return refusal
    ident = approvals.create(ACTION, {"month": month})
    return {"status": "pending_approval", "approval_id": ident, "month": month,
            "text": (f"להריץ עכשיו דוח בנק הפועלים לחודש {month}? התחברות אחת לבנק מה-NUC; "
                     "אם הבנק יבקש, יישלח אליך SMS והקוד יוזן אוטומטית פעם אחת. ה-CSV נשאר ב-NUC.")}


def execute_run(payload):
    """Approved handler: re-check the limits, then queue ONE run for the host runner. Never runs the bank here."""
    month = str((payload or {}).get("month") or "")
    if not MONTH_RE.fullmatch(month):
        raise ValueError("bad month")
    with _lock:
        refusal = _refusal()
        if refusal:
            raise ValueError(refusal["status"])
        request = {"run_id": os.urandom(8).hex(), "month": month, "approved_at": time.time()}
        _atomic_write(SPOOL_DIR / "request.json", request)
    return {"queued": True, "run_id": request["run_id"], "month": month}


# --------------------------------------------------------------------------- one-code broker

def _used_marker(run_id):
    return SPOOL_DIR / f"otp-used-{run_id}"


def broker_handle(message, now=None, sleep=time.sleep, clock=time.time):
    """One request from the scraper -> one reply. Ops:
      {"op":"otp_required","run_id"}                -> {"ok":true}  (records that the bank asked for an SMS)
      {"op":"take_code","run_id","since","wait"}    -> {"ok":true,"code"} | {"ok":false,"error":...}
    Errors: BAD_REQUEST, NOT_ARMED (no approved run with this id in its window), ALREADY_USED, OTP_TIMEOUT,
    OTP_AMBIGUOUS. The code is never logged."""
    if not isinstance(message, dict):
        return {"ok": False, "error": "BAD_REQUEST"}
    op, run_id = message.get("op"), str(message.get("run_id") or "")
    if op not in ("otp_required", "take_code") or not RUN_ID_RE.fullmatch(run_id):
        return {"ok": False, "error": "BAD_REQUEST"}
    active = _active_request(now if now is not None else clock())
    if not active or active["run_id"] != run_id:
        return {"ok": False, "error": "NOT_ARMED"}
    if op == "otp_required":
        _atomic_write(SPOOL_DIR / f"otp-requested-{run_id}", {"at": clock()})
        return {"ok": True}
    try:
        since = float(message.get("since"))
        wait = min(max(float(message.get("wait", 120)), 0.0), MAX_WAIT)
    except (TypeError, ValueError):
        return {"ok": False, "error": "BAD_REQUEST"}
    since = max(since, float(active["approved_at"]))      # never a code from before the approval
    with _lock:
        if _used_marker(run_id).exists():
            return {"ok": False, "error": "ALREADY_USED"}
        _atomic_write(_used_marker(run_id), {"at": clock()})   # one take per run, whatever happens next
    deadline = clock() + wait
    while True:
        code, reason = otp_inbox.take_unique(INBOX_DB, SITE, since=since, now=clock())
        if code is not None:
            return {"ok": True, "code": code}
        if reason == "ambiguous":
            return {"ok": False, "error": "OTP_AMBIGUOUS"}
        if clock() >= deadline:
            return {"ok": False, "error": "OTP_TIMEOUT"}
        sleep(POLL)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(MAX_WAIT + 30)
        try:
            line = self.rfile.readline(4096)
            reply = broker_handle(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError, OSError):
            reply = {"ok": False, "error": "BAD_REQUEST"}
        try:
            self.wfile.write((json.dumps(reply) + "\n").encode())
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def start_broker():
    """Listen on BROKER_SOCK when the broker dir is mounted (no-op otherwise). Socket: root:<scraper gid> 0660."""
    global _server
    if _server is not None or not BROKER_DIR.is_dir():
        return None
    try:
        BROKER_SOCK.unlink()
    except FileNotFoundError:
        pass
    _server = _Server(str(BROKER_SOCK), _Handler)
    try:
        os.chown(BROKER_SOCK, -1, SCRAPER_GID)
    except PermissionError:
        pass
    os.chmod(BROKER_SOCK, 0o660)
    threading.Thread(target=_server.serve_forever, name="hapoalim-otp-broker", daemon=True).start()
    return _server


def stop_broker():
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None


# --------------------------------------------------------------------------- wiring

def register_routes(router):
    router.get("/v1/hapoalim/status", status)
    router.post("/v1/proposals/hapoalim-run", propose_run, 202)


def register_actions():
    approvals.register_action(ACTION, execute_run)
