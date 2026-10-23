"""Cal (visaCal) credit-card on-demand sync: status, approval-gated run, archive reads for Hermes.

Mirrors hapoalim.py. The Command Center never sees the Cal credentials, codes or the browser. It:
  * shows a sanitized status (copied by the root host runner cal-runner.py into the spool);
  * proposes a sync (Ben approves EVERY time; approvals action ``card_sync``), which only drops
    ``request.json`` into the Cal spool; the host runner (systemd path unit cal-run.path) does the rest;
  * refuses early (no approval card, nothing spooled) when the archive already covers the months (COVERED),
    when today is stopped after an auth/OTP failure, when a run is pending, or when the daily login limit
    (CAL_MAX_RUNS_PER_DAY on the host, default 2, published in runs-today.json) is used up;
  * reads the monthly card archive (billing months) for Hermes.
On-demand only — no schedule.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import approvals
from . import cal_archive as archive

ACTION = "card_sync"
TZ = ZoneInfo("Asia/Jerusalem")
SPOOL_DIR = Path(os.environ.get("CAL_SPOOL_DIR", "/cal-spool"))
ARCHIVE_DIR = Path(os.environ.get("CAL_ARCHIVE_DIR", "/cal-archive"))
DEFAULT_MONTHS = int(os.environ.get("CAL_MONTHS", "3") or "3")
LOOKBACK_MONTHS = 18
RUN_WINDOW = 30 * 60
YM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
# The runner's built-in default (CAL_MAX_RUNS_PER_DAY in cal-runner.py); used only before the runner has
# published runs-today.json. The real max comes from the host unit env / Ben's limits.conf drop-in.
RUNNER_DEFAULT_MAX_RUNS = 2
STATUS_FIELDS = ("site", "status", "stage", "stopForToday", "blocked", "otpRequested", "otpDelivered",
                 "blockedHosts", "failedHosts", "apiHosts", "blockedApiHosts", "seconds", "cardCount",
                 "transactionCount", "pendingCount", "monthsCovered", "from_month", "to_month", "cards",
                 "profileWiped", "fakeCal", "warnings", "runsToday", "maxRuns", "coverage", "archiveNew",
                 "archiveRemoved", "archiveMonths", "archiveCompleteMonths", "archivePendingRows", "archiveWarnings",
                 "archiveSkipped", "csv", "csvRows", "files", "actions", "otpWait", "runId", "finishedAt",
                 "requestedAt")
ERROR_TEXT_HE = {
    "INVALID_PASSWORD": "כאל דחתה את שם המשתמש או הסיסמה. ננעל להיום: לא ננסה שוב עד שבן יבדוק.",
    "CHANGE_PASSWORD": "כאל מבקשת להחליף סיסמה. ננעל להיום.",
    "ACCOUNT_BLOCKED": "כאל חסמה את הגישה לחשבון. ננעל להיום.",
    "LOGIN_UNKNOWN": "ההתחברות לכאל לא הצליחה מסיבה לא ידועה. ננעל להיום.",
    "CAPTCHA_OR_BLOCK": "כאל הציגה בדיקת רובוט/חסימה. ננעל להיום.",
    "CAL_BLOCKED": "כאל חסמה את הבקשות (429). מה שהגיע נשמר; ננעל להיום.",
    "OTP_REQUIRED": "כאל ביקשה קוד SMS והזנה אוטומטית כבויה. ננעל להיום.",
    "OTP_REJECTED": "כאל דחתה את קוד ה-SMS (ניסיון אחד בלבד). ננעל להיום.",
    "OTP_NO_RESULT": "לא הייתה תשובה מכאל אחרי קוד ה-SMS. ננעל להיום.",
    "OTP_FORM_CHANGED": "טופס ה-SMS של כאל לא מוכר. ננעל להיום עד עדכון.",
    "OTP_TIMEOUT": "קוד ה-SMS של כאל לא הגיע בזמן דרך ה-relay. אפשר לנסות מחר.",
    "OTP_AMBIGUOUS": "הגיעו שני קודים שונים, לא השתמשתי באף אחד. אפשר לנסות מחר.",
    "CARDS_FAILED": "ההתחברות הצליחה אבל קריאת הכרטיסים נכשלה. ננעל להיום.",
    "NO_CARDS": "לא נמצאו כרטיסים בחשבון כאל. ננעל להיום.",
    "STOPPED_TODAY": "נעול להיום אחרי כשל הזדהות/OTP בכאל. לא ננסה שוב היום.",
    "DAILY_LIMIT": "הגעת למכסת ההתחברויות לכאל להיום. המונה מתאפס בחצות; אפשר לנסות שוב מחר (נתונים מהארכיון זמינים גם עכשיו).",
    "LOCKED": "ריצה אחרת של כאל כבר רצה; נסה שוב בעוד דקה.",
    "NOT_CONFIGURED": "חסרות הגדרות כאל ב-NUC (סודות/image). בן צריך להריץ cal-setup.",
    "BAD_ACTIONS": "בקשת הפעולות לא תקינה.",
    "ARCHIVE_WRITE_FAILED": "החיובים נשמרו ב-CSV ב-out/ אבל לא נכנסו לארכיון החודשי (הכיסוי לא עודכן).",
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
            approved = float(data.get("requested_at") or 0)
            if 0 < approved <= now + 5 and now - approved <= RUN_WINDOW:
                return dict(data, stage="running" if name == "running.json" else "queued")
    return None


def _stopped_today():
    last = _read_json(SPOOL_DIR / "last-status.json") or {}
    if last.get("stopForToday") is True:
        try:
            finished = float(last.get("finishedAt") or 0)
        except (TypeError, ValueError):
            finished = 0
        if finished and _today(finished) == _today():
            return str(last.get("status") or "STOPPED_TODAY")
    return None


def _next_midnight_iso(now=None):
    now_dt = datetime.fromtimestamp(time.time() if now is None else now, TZ)
    return (now_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _daily_limit(now=None):
    """Same contract as hapoalim._daily_limit: spool/runs-today.json from the runner is the source;
    a corrupt file modified today fails CLOSED; yesterday's counter is ignored (its max_runs still used)."""
    now = time.time() if now is None else now
    today = _today(now)
    path = SPOOL_DIR / "runs-today.json"
    out = {"runs_today": 0, "max_runs": RUNNER_DEFAULT_MAX_RUNS, "max_runs_source": "default",
           "runs_state": "missing", "limit_reached": False, "resets_at": _next_midnight_iso(now)}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except (OSError, ValueError):
            try:
                fresh = _today(path.stat().st_mtime) == today
            except OSError:
                fresh = True
            if fresh:
                out.update(runs_state="corrupt", limit_reached=True)
                return out
            out["runs_state"] = "stale_corrupt"
            return out
        mx = data.get("max_runs")
        if isinstance(mx, int) and not isinstance(mx, bool) and 1 <= mx <= 100:
            out.update(max_runs=mx, max_runs_source="runner")
        runs = data.get("runs")
        if data.get("date") == today:
            if not (isinstance(runs, int) and not isinstance(runs, bool) and runs >= 0):
                out.update(runs_state="corrupt", limit_reached=True)
                return out
            out.update(runs_today=runs, runs_state="ok")
        else:
            out["runs_state"] = "previous_day"
    if out["runs_today"] > 0 and out["runs_today"] >= out["max_runs"]:
        out["limit_reached"] = True
    return out


def _limit_result(lim):
    return {"status": "DAILY_LIMIT", "runs_today": lim["runs_today"], "max_runs": lim["max_runs"],
            "max_runs_source": lim["max_runs_source"], "resets_at": lim["resets_at"], "retry_today": False,
            "text": ERROR_TEXT_HE["DAILY_LIMIT"] + f" ({lim['runs_today']}/{lim['max_runs']} היום)"}


def status(_payload=None, _query=None):
    last = _read_json(SPOOL_DIR / "last-status.json") or {}
    last = {k: last[k] for k in STATUS_FIELDS if k in last}
    active = _active_request()
    stopped = _stopped_today()
    lim = _daily_limit()
    act0 = ((active or {}).get("actions") or [{}])[0] if active else {}
    out = {
        "site": "cal",
        "last_run": last or None,
        "pending_run": {"run_id": active["run_id"], "stage": active["stage"],
                        "from_month": act0.get("from_month"), "to_month": act0.get("to_month")} if active else None,
        "stopped_today": stopped,
        "can_run_today": not stopped and not active and not lim["limit_reached"],
        "runs_today": lim["runs_today"], "max_runs": lim["max_runs"], "max_runs_source": lim["max_runs_source"],
        "limit_reached": lim["limit_reached"], "runs_state": lim["runs_state"], "resets_at": lim["resets_at"],
    }
    if lim["limit_reached"]:
        out["error_text"] = _limit_result(lim)["text"]
    elif last.get("status") in ERROR_TEXT_HE and last.get("status") != "DAILY_LIMIT":
        out["error_text"] = ERROR_TEXT_HE[last["status"]]
    elif stopped and stopped in ERROR_TEXT_HE:
        out["error_text"] = ERROR_TEXT_HE[stopped]
    return out


def _month_range(from_m, to_m, now=None):
    """-> (from, to, warnings) or raises ValueError. Defaults: last CAL_MONTHS statements + the next one."""
    cur = archive.month_il(now)
    hi, lo = archive.add_months(cur, 1), archive.add_months(cur, -LOOKBACK_MONTHS)
    for name, v in (("from_month", from_m), ("to_month", to_m)):
        if v and not YM.fullmatch(v):
            raise ValueError(f"{name} must be YYYY-MM")
    to_m = to_m or hi
    from_m = from_m or archive.add_months(cur, -(max(1, DEFAULT_MONTHS) - 1))
    warnings = []
    if to_m > hi:
        warnings.append(f"to_month clamped to {hi}")
        to_m = hi
    if from_m < lo:
        warnings.append(f"from_month clamped to {lo} (Cal keeps ~{LOOKBACK_MONTHS} months)")
        from_m = lo
    if from_m > to_m:
        raise ValueError("from_month must be <= to_month")
    return from_m, to_m, warnings


def _plan_or_none(from_m, to_m):
    if not ARCHIVE_DIR.is_dir():
        return None
    try:
        return archive.plan_gaps(ARCHIVE_DIR, from_m, to_m)
    except Exception:
        return None


def propose_sync(payload, _query=None):
    payload = payload or {}
    try:
        from_m, to_m, warnings = _month_range(str(payload.get("from_month") or "").strip() or None,
                                              str(payload.get("to_month") or "").strip() or None)
    except ValueError as exc:
        return {"status": "bad_request", "text": str(exc)}
    stopped = _stopped_today()
    if stopped:
        return {"status": "stopped_today", "text": ERROR_TEXT_HE.get(stopped, ERROR_TEXT_HE["STOPPED_TODAY"])}
    if _active_request():
        return {"status": "already_pending", "text": "יש כבר סנכרון כאל מאושר שממתין או רץ."}
    plan = _plan_or_none(from_m, to_m)
    if plan and plan.get("covered"):
        return {"status": "COVERED", "from_month": from_m, "to_month": to_m, "warnings": warnings,
                "cards": plan.get("cards"),
                "text": (f"חודשי החיוב {from_m} עד {to_m} כבר בארכיון המקומי של כאל — בלי התחברות. "
                         "אפשר לקרוא עם card_read_month / card_read_range.")}
    lim = _daily_limit()
    if lim["limit_reached"]:
        return _limit_result(lim)
    fetch_from = (plan or {}).get("fetch_from") or from_m
    fetch_to = (plan or {}).get("fetch_to") or to_m
    actions = [{"name": "card_transactions", "from_month": fetch_from, "to_month": fetch_to}]
    ident = approvals.create(ACTION, {"actions": actions, "from_month": from_m, "to_month": to_m,
                                      "fetch_from": fetch_from, "fetch_to": fetch_to})
    text = (f"לסנכרן עכשיו את כרטיסי האשראי של כאל (חודשי חיוב {fetch_from} עד {fetch_to})? התחברות אחת מה-NUC; "
            "אם כאל תבקש קוד, יישלח אליך SMS והקוד יוזן אוטומטית פעם אחת. "
            "אחרי זה החיובים יישמרו בארכיון החודשי.")
    if warnings:
        text += " (" + "; ".join(warnings) + ")"
    return {"status": "pending_approval", "approval_id": ident, "from_month": from_m, "to_month": to_m,
            "fetch_from": fetch_from, "fetch_to": fetch_to, "needed": (plan or {}).get("needed"),
            "warnings": warnings, "text": text}


def execute_sync(payload):
    """Approved handler: queue ONE run for the host runner. Never talks to Cal here."""
    actions = (payload or {}).get("actions")
    if not isinstance(actions, list) or not actions or any(
            not isinstance(a, dict) or a.get("name") != "card_transactions" for a in actions):
        raise ValueError("bad actions")
    with _lock:
        if _stopped_today():
            raise ValueError("stopped_today")
        if _active_request():
            raise ValueError("already_pending")
        if _daily_limit()["limit_reached"]:
            raise ValueError("daily_limit")
        request = {"run_id": os.urandom(8).hex(), "requested_at": time.time(), "actions": actions}
        _atomic_write(SPOOL_DIR / "request.json", request)
    return {"queued": True, "run_id": request["run_id"],
            "text": "סנכרון כאל בתור. אם כאל תשלח SMS — הקוד ייכנס אוטומטית. בדוק card_status בעוד כדקה."}


def list_archive(_payload=None, _query=None):
    if not ARCHIVE_DIR.is_dir():
        return {"status": "no_archive", "files": [], "text": "אין ארכיון כאל עדיין."}
    report = archive.list_archive(ARCHIVE_DIR)
    report["text"] = f"{len(report.get('files') or [])} קבצים בארכיון כאל."
    return report


def read_month(payload, _query=None):
    payload = payload or {}
    ym = str(payload.get("month") or payload.get("yyyy_mm") or "").strip()
    card = str(payload.get("card") or "").strip() or None
    if not ym:
        return {"status": "bad_request", "text": "month (YYYY-MM) required"}
    try:
        return archive.read_month(ARCHIVE_DIR, ym, card=card, include_pending=bool(payload.get("include_pending")))
    except ValueError as exc:
        return {"status": "bad_request", "text": str(exc)}


def read_range(payload, _query=None):
    payload = payload or {}
    from_s = str(payload.get("from") or "").strip()
    to_s = str(payload.get("to") or "").strip()
    card = str(payload.get("card") or "").strip() or None
    by = str(payload.get("by") or "charge").strip()
    if not (ISO.fullmatch(from_s) and ISO.fullmatch(to_s)):
        return {"status": "bad_request", "text": "from/to must be YYYY-MM-DD"}
    try:
        return archive.read_range(ARCHIVE_DIR, from_s, to_s, card=card, by=by)
    except ValueError as exc:
        return {"status": "bad_request", "text": str(exc)}


def coverage(_payload=None, _query=None):
    if not ARCHIVE_DIR.is_dir():
        return {"status": "no_archive", "text": "אין ארכיון כאל עדיין."}
    return archive.coverage_report(ARCHIVE_DIR)


def register_routes(router):
    router.get("/v1/cal/status", status)
    router.post("/v1/proposals/card-sync", propose_sync, 202)
    router.get("/v1/cal/list-archive", list_archive)
    router.post("/v1/cal/read-month", read_month)
    router.post("/v1/cal/read-range", read_range)
    router.get("/v1/cal/coverage", coverage)


def register_actions():
    approvals.register_action(ACTION, execute_sync)
