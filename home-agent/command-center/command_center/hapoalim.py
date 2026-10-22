"""Bank Hapoalim on-demand sync: status, approval-gated run (actions in one login), CSV read for Hermes.

The Command Center never sees the bank credentials or the browser profile. It:
  * shows a sanitized status (copied by the host runner into the spool; no secrets);
  * proposes a sync (Ben must approve EVERY time; approvals action ``hapoalim_sync``), which only drops
    ``request.json`` into the spool; the root host runner (systemd path unit) starts the one-shot container;
  * serves the scraper ONE relayed SMS code via the shared OTP inbox (Hermes otp-relay → codes.sqlite),
    read by the host runner as the inbox owner (same mechanism as super_hist; site=hapoalim only);
  * lets Hermes read CSV + monthly archive (list/read_month/read_range); returns COVERED when archive covers the range.

Lockout / stop-for-today / concurrent lock live in the host runner; status mirrors them so a proposal can be
refused early. On-demand only — no schedule, no timer.
"""

import csv
import io
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import approvals
from . import hapoalim_archive as archive

ACTION = "hapoalim_sync"
TZ = ZoneInfo("Asia/Jerusalem")
SPOOL_DIR = Path(os.environ.get("HAPOALIM_SPOOL_DIR", "/hapoalim-spool"))
OUT_DIR = Path(os.environ.get("HAPOALIM_OUT_DIR", "/hapoalim-out"))
ARCHIVE_DIR = Path(os.environ.get("HAPOALIM_ARCHIVE_DIR", "/hapoalim-archive"))
REPULL_DAYS = int(os.environ.get("HAPOALIM_REPULL_DAYS", "3") or "3")
RUN_WINDOW = 30 * 60
# Library lookback cap ≈ today-1y+1d (israeli-bank-scrapers 6.9.0).
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
CSV_NAME_RE = re.compile(r"^hapoalim-\d{8}-\d{6}(?:-[a-z0-9_]+)*(?:-[0-9a-f]{4})?\.csv$")
# Status fields that may leave the spool (everything else is dropped). No amounts/descriptions here —
# those live only in the CSV that Hermes reads explicitly.
STATUS_FIELDS = ("status", "stage", "stopForToday", "otpRequested", "otpDelivered", "blockedHosts",
                 "failedHosts", "seconds", "accountCount", "transactionCount", "firstDate", "lastDate",
                 "monthsCovered", "from", "to", "profileWiped", "fakeBank", "warnings", "csv", "csvPath",
                 "csvRows", "files", "actions", "otpWait", "runId", "finishedAt", "requestedAt",
                 "archiveNew", "archiveUpdated", "archiveSuperseded", "archiveMonths", "archiveSkipped",
                 "coverage")
ERROR_TEXT_HE = {
    "INVALID_PASSWORD": "הבנק דחה את הסיסמה. ננעל להיום: לא ננסה שוב עד שבן יבדוק.",
    "CHANGE_PASSWORD": "הבנק מבקש להחליף סיסמה. ננעל להיום.",
    "ACCOUNT_BLOCKED": "הבנק חסם את החשבון לגישה. ננעל להיום.",
    "OTP_REJECTED": "הבנק דחה את קוד ה-SMS (ניסיון אחד בלבד). ננעל להיום.",
    "OTP_NO_RESULT": "לא הייתה תשובה מהבנק אחרי קוד ה-SMS. ננעל להיום.",
    "OTP_FORM_CHANGED": "טופס ה-SMS של הבנק השתנה. ננעל להיום עד עדכון.",
    "OTP_TIMEOUT": "קוד ה-SMS לא הגיע בזמן דרך ה-relay. אפשר לנסות מחר.",
    "OTP_AMBIGUOUS": "הגיעו שני קודים שונים, לא השתמשתי באף אחד. אפשר לנסות מחר.",
    "STOPPED_TODAY": "נעול להיום אחרי כשל הזדהות/OTP. לא ננסה שוב היום.",
    "DAILY_LIMIT": "הגעת למכסת הריצות להיום.",
    "LOCKED": "ריצה אחרת כבר רצה; נסה שוב בעוד דקה.",
    "NOT_CONFIGURED": "חסרות הגדרות ב-NUC (סודות/image). בן צריך להריץ hapoalim-setup.",
    "BAD_ACTIONS": "בקשת הפעולות לא תקינה.",
    "ARCHIVE_WRITE_FAILED": "התנועות נשמרו ב-CSV ב-out/ אבל לא נכנסו לארכיון החודשי (הכיסוי לא עודכן). אפשר לייבא עם hapoalim-archive-import.",
}

_lock = threading.Lock()


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


def _active_request(now=None):
    now = time.time() if now is None else now
    for name in ("running.json", "request.json"):
        data = _read_json(SPOOL_DIR / name)
        if data and RUN_ID_RE.fullmatch(str(data.get("run_id", ""))):
            approved = float(data.get("requested_at") or data.get("approved_at") or 0)
            if 0 < approved <= now + 5 and now - approved <= RUN_WINDOW:
                return dict(data, stage="running" if name == "running.json" else "queued")
    return None


def _stopped_today():
    last = _read_json(SPOOL_DIR / "last-status.json") or {}
    if last.get("stopForToday") and str(last.get("finishedAt") or "").startswith(""):
        # Prefer the runner's stop file mirror if present; else infer from today's last status.
        pass
    stop = SPOOL_DIR / f"stop-{_today()}"
    # The runner writes stop files under STATE_DIR; it also sets stopForToday on last-status.
    if last.get("stopForToday") is True:
        finished = float(last.get("finishedAt") or 0)
        if finished and _today(finished) == _today():
            return str(last.get("status") or "STOPPED_TODAY")
    return None


def status(_payload=None, _query=None):
    last = _read_json(SPOOL_DIR / "last-status.json") or {}
    last = {key: last[key] for key in STATUS_FIELDS if key in last}
    active = _active_request()
    stopped = _stopped_today()
    out = {
        "last_run": last or None,
        "pending_run": {"run_id": active["run_id"], "stage": active["stage"],
                        "from": (active.get("actions") or [{}])[0].get("from") if active.get("actions") else None,
                        "to": (active.get("actions") or [{}])[0].get("to") if active.get("actions") else None}
        if active else None,
        "stopped_today": stopped,
        "can_run_today": not stopped and not active,
        "out_dir": str(OUT_DIR),
    }
    if last.get("status") in ERROR_TEXT_HE:
        out["error_text"] = ERROR_TEXT_HE[last["status"]]
    elif stopped and stopped in ERROR_TEXT_HE:
        out["error_text"] = ERROR_TEXT_HE[stopped]
    return out



def _default_range(from_s, to_s):
    today = _today()
    to_s = to_s or today
    warnings = []
    if not from_s:
        from datetime import timedelta
        d = datetime.now(TZ).date() - timedelta(days=int(os.environ.get("HAPOALIM_DAYS", "45") or "45"))
        from_s = d.isoformat()
    # Clamp to library cap (today - 1 year + 1 day).
    from datetime import date as _date, timedelta as _td
    t = datetime.now(TZ).date()
    try:
        cap_d = t.replace(year=t.year - 1)
    except ValueError:  # Feb 29
        cap_d = _date(t.year - 1, 2, 28)
    cap = (cap_d + _td(days=1)).isoformat()
    if from_s < cap:
        warnings.append(f"from clamped to library max lookback {cap} (requested {from_s})")
        from_s = cap
    if to_s > today:
        to_s = today
    return from_s, to_s, warnings


def _plan_or_none(from_s, to_s):
    if not ARCHIVE_DIR.is_dir():
        return None
    try:
        return archive.plan_gaps(ARCHIVE_DIR, from_s, to_s, repull_days=max(0, REPULL_DAYS))
    except Exception:
        return None


def _validate_range(from_s, to_s):
    """Light check at propose-time; the container re-validates and clamps (lib/dates.mjs)."""
    if from_s is not None and from_s != "" and not ISO.fullmatch(str(from_s)):
        return "from must be YYYY-MM-DD"
    if to_s is not None and to_s != "" and not ISO.fullmatch(str(to_s)):
        return "to must be YYYY-MM-DD"
    if from_s and to_s and str(from_s) > str(to_s):
        return "from must be <= to"
    today = _today()
    if from_s and str(from_s) > today:
        return "from is in the future"
    return None


def propose_sync(payload, _query=None):
    payload = payload or {}
    from_s = str(payload.get("from") or "").strip() or None
    to_s = str(payload.get("to") or "").strip() or None
    err = _validate_range(from_s, to_s)
    if err:
        return {"status": "bad_request", "text": err}
    # Optional future: payload.actions = [{name, ...}]; default = checking_transactions.
    actions = payload.get("actions")
    if actions is not None:
        if not isinstance(actions, list) or not actions:
            return {"status": "bad_request", "text": "actions must be a non-empty list"}
        for a in actions:
            if not isinstance(a, dict) or not str(a.get("name") or "").strip():
                return {"status": "bad_request", "text": "each action needs a name"}
    else:
        actions = [{"name": "checking_transactions"}]
        if from_s:
            actions[0]["from"] = from_s
        if to_s:
            actions[0]["to"] = to_s

    stopped = _stopped_today()
    if stopped:
        return {"status": "stopped_today", "text": ERROR_TEXT_HE.get(stopped, ERROR_TEXT_HE["STOPPED_TODAY"])}
    if _active_request():
        return {"status": "already_pending", "text": "יש כבר סנכרון מאושר שממתין או רץ."}

    req_from, req_to, clamp_warnings = _default_range(from_s, to_s)
    plan = _plan_or_none(req_from, req_to)
    if plan and plan.get("covered"):
        return {
            "status": "COVERED",
            "from": req_from, "to": req_to,
            "warnings": clamp_warnings,
            "coverage": {"gaps": [], "accounts": plan.get("accounts"), "repull_days": plan.get("repull_days")},
            "text": (f"הטווח {req_from} עד {req_to} כבר בארכיון המקומי — בלי התחברות לבנק. "
                     "אפשר לקרוא עם hapoalim_read_month / hapoalim_read_range / hapoalim_list_archive."),
        }
    fetch_from = (plan or {}).get("fetch_from") or req_from
    fetch_to = (plan or {}).get("fetch_to") or req_to
    for a in actions:
        if isinstance(a, dict) and a.get("name") == "checking_transactions":
            a["from"] = fetch_from
            a["to"] = fetch_to

    window = f"{fetch_from} עד {fetch_to}"
    ident = approvals.create(ACTION, {"actions": actions, "from": from_s, "to": to_s,
                                      "fetch_from": fetch_from, "fetch_to": fetch_to})
    text = (f"לסנכרן עכשיו את עו״ש בנק הפועלים ({window})? התחברות אחת מה-NUC; "
            "אם הבנק יבקש, יישלח אליך SMS והקוד יוזן אוטומטית פעם אחת. "
            "אחרי זה הנתונים יישמרו בארכיון החודשי.")
    if clamp_warnings:
        text += " (" + "; ".join(clamp_warnings) + ")"
    return {
        "status": "pending_approval", "approval_id": ident,
        "from": from_s, "to": to_s, "fetch_from": fetch_from, "fetch_to": fetch_to,
        "actions": [a.get("name") for a in actions],
        "gaps": (plan or {}).get("gaps"),
        "warnings": clamp_warnings,
        "text": text,
    }


def execute_sync(payload):
    """Approved handler: queue ONE run for the host runner. Never talks to the bank here."""
    actions = (payload or {}).get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("bad actions")
    with _lock:
        if _stopped_today():
            raise ValueError("stopped_today")
        if _active_request():
            raise ValueError("already_pending")
        request = {
            "run_id": os.urandom(8).hex(),
            "requested_at": time.time(),
            "actions": actions,
        }
        _atomic_write(SPOOL_DIR / "request.json", request)
    return {"queued": True, "run_id": request["run_id"],
            "text": "הסנכרון בתור. אם הבנק ישלח SMS — הקוד ייכנס אוטומטית. בדוק hapoalim_status בעוד כדקה."}


def _safe_csv_path(name):
    if not isinstance(name, str) or not CSV_NAME_RE.fullmatch(name):
        raise ValueError("bad csv name")
    path = (OUT_DIR / name).resolve()
    if OUT_DIR.resolve() not in path.parents and path != OUT_DIR.resolve():
        raise ValueError("csv path escapes out dir")
    if not path.is_file():
        raise ValueError("csv not found")
    return path


def read_csv(payload, _query=None):
    """Return the full CSV text (and parsed rows) for Hermes. Ben decided full details may reach the LLM."""
    payload = payload or {}
    name = payload.get("name") or (_read_json(SPOOL_DIR / "last-status.json") or {}).get("csv")
    if not name:
        return {"status": "no_csv", "text": "אין CSV אחרון. הרץ hapoalim_sync קודם."}
    try:
        path = _safe_csv_path(name)
    except ValueError as exc:
        return {"status": "bad_request", "text": str(exc)}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"status": "not_found", "text": "לא הצלחתי לקרוא את הקובץ."}
    if len(text) > 2_000_000:
        return {"status": "too_large", "text": "הקובץ גדול מדי לקריאה ישירה."}
    rows = list(csv.DictReader(io.StringIO(text)))
    return {
        "status": "ok",
        "name": name,
        "path": str(path),
        "rows": rows,
        "row_count": len(rows),
        "text": f"קראתי {len(rows)} תנועות מ-{name}.",
    }



def list_archive(_payload=None, _query=None):
    if not ARCHIVE_DIR.is_dir():
        return {"status": "no_archive", "files": [], "text": "אין ארכיון עדיין."}
    report = archive.list_archive(ARCHIVE_DIR)
    report["text"] = f"{len(report.get('files') or [])} קבצי חודש בארכיון."
    # Drop full coverage blob from tool response (keep names/meta only)
    report.pop("coverage", None)
    return report


def read_month(payload, _query=None):
    payload = payload or {}
    ym = str(payload.get("month") or payload.get("yyyy_mm") or "").strip()
    account = str(payload.get("account") or "").strip() or None
    if not ym:
        return {"status": "bad_request", "text": "month (YYYY-MM) required"}
    try:
        return archive.read_month(ARCHIVE_DIR, ym, account=account)
    except ValueError as exc:
        return {"status": "bad_request", "text": str(exc)}


def read_range(payload, _query=None):
    payload = payload or {}
    from_s = str(payload.get("from") or "").strip()
    to_s = str(payload.get("to") or "").strip()
    account = str(payload.get("account") or "").strip() or None
    if not from_s or not to_s:
        from_s, to_s, _ = _default_range(from_s or None, to_s or None)
    err = _validate_range(from_s, to_s)
    if err:
        return {"status": "bad_request", "text": err}
    try:
        return archive.read_range(ARCHIVE_DIR, from_s, to_s, account=account)
    except ValueError as exc:
        return {"status": "bad_request", "text": str(exc)}


def coverage(_payload=None, _query=None):
    if not ARCHIVE_DIR.is_dir():
        return {"status": "no_archive", "files": [], "text": "אין ארכיון עדיין."}
    return archive.coverage_report(ARCHIVE_DIR)


def register_routes(router):
    router.get("/v1/hapoalim/status", status)
    router.post("/v1/proposals/hapoalim-sync", propose_sync, 202)
    router.post("/v1/hapoalim/read-csv", read_csv)
    router.get("/v1/hapoalim/list-archive", list_archive)
    router.post("/v1/hapoalim/read-month", read_month)
    router.post("/v1/hapoalim/read-range", read_range)
    router.get("/v1/hapoalim/coverage", coverage)


def register_actions():
    approvals.register_action(ACTION, execute_sync)


# Kept so older imports of start_broker don't crash; OTP is now read by the host runner from codes.sqlite.
def start_broker():
    return None
