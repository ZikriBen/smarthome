"""Credit-card on-demand sync (multi-account; provider "cal" = visaCal): status, approval-gated run, archive
reads for Hermes. Every call takes an optional `account` (card_accounts.ACCOUNT_RE; default cal-main = the
pre-multi account, so the old calls/outputs are unchanged; new keys only). Accounts come from the runner's
sanitized registry mirror spool/accounts.json (missing = cal-main only). Per account: spool/accounts/<id>/
(last-status.json, runs-today.json) and archive/cards/<id>/. Until card-migrate.py ran on the host, cal-main
is read from the old root layout (read-only fallback). One run at a time for ALL accounts (one bank login).

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
from . import card_accounts as accounts

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
                 "requestedAt", "account", "provider", "runsTotal", "maxRunsTotal")
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
    "LOCKED": "ריצה אחרת (כרטיס אחר או בנק הפועלים) כבר רצה; נסה שוב בעוד דקה.",
    "BAD_ACCOUNT": "מזהה חשבון כרטיס לא תקין.",
    "UNKNOWN_ACCOUNT": "חשבון הכרטיס לא מוגדר ב-NUC. בן צריך להריץ sudo card-setup <account>.",
    "NOT_SUPPORTED": "חברת הכרטיס הזו עוד לא נתמכת (כרגע רק כאל).",
    "ACCOUNT_DISABLED": "חשבון הכרטיס מושבת ברישום החשבונות ב-NUC.",
    "BAD_REGISTRY": "קובץ רישום החשבונות ב-NUC פגום. ננעל להיום עד שבן יתקן.",
    "MIGRATION_PENDING": "צריך להריץ את העברת הארכיון לחשבונות (sudo card-migrate --apply) לפני סנכרון.",
    "AMBIGUOUS_CARD": "אותן 4 ספרות קיימות ביותר מחשבון אחד; צריך לציין account.",
    "NOT_CONFIGURED": "חסרות הגדרות לחשבון הכרטיס ב-NUC (סודות/image). בן צריך להריץ sudo card-setup <account>.",
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


def _registry():
    """The runner's sanitized mirror; missing -> cal-main only. Never trusted for credentials (none in it)."""
    data = _read_json(SPOOL_DIR / "accounts.json")
    if not data:
        return {"default": accounts.DEFAULT_ACCOUNT, "max_runs_total": 0, "mirror": "missing",
                "accounts": [{"id": accounts.DEFAULT_ACCOUNT, "provider": "cal", "label": "Cal", "enabled": True,
                              "supported": True, "max_runs_per_day": RUNNER_DEFAULT_MAX_RUNS}]}
    out = {"default": accounts.DEFAULT_ACCOUNT, "mirror": "ok", "accounts": []}
    mx = data.get("max_runs_total")
    out["max_runs_total"] = mx if isinstance(mx, int) and not isinstance(mx, bool) and 0 <= mx <= 100 else 0
    for a in data.get("accounts") or []:
        if isinstance(a, dict) and accounts.ACCOUNT_RE.fullmatch(str(a.get("id", ""))):
            prov = str(a.get("provider") or "")
            out["accounts"].append({"id": a["id"], "provider": prov, "label": str(a.get("label") or a["id"])[:40],
                                    "enabled": a.get("enabled") is True,
                                    "supported": prov in accounts.PROVIDERS,
                                    "max_runs_per_day": a.get("max_runs_per_day")})
    if not out["accounts"]:
        out["accounts"] = [{"id": accounts.DEFAULT_ACCOUNT, "provider": "cal", "label": "Cal", "enabled": True,
                            "supported": True, "max_runs_per_day": RUNNER_DEFAULT_MAX_RUNS}]
        out["mirror"] = "empty"
    return out


class AccountError(ValueError):
    def __init__(self, code, text, **extra):
        super().__init__(text)
        self.code, self.text, self.extra = code, text, extra

    def result(self):
        return dict({"status": self.code, "text": self.text}, **self.extra)


def _account_ids():
    return [a["id"] for a in _registry()["accounts"]]


def _entry(account):
    """-> registry entry for `account` ('' -> cal-main) or AccountError."""
    account = str(account or "").strip() or accounts.DEFAULT_ACCOUNT
    if not accounts.ACCOUNT_RE.fullmatch(account):
        raise AccountError("BAD_ACCOUNT", ERROR_TEXT_HE["BAD_ACCOUNT"] + " (דוגמה: cal-main, cal-2)")
    for a in _registry()["accounts"]:
        if a["id"] == account:
            return a
    raise AccountError("UNKNOWN_ACCOUNT", ERROR_TEXT_HE["UNKNOWN_ACCOUNT"], account=account,
                       known_accounts=_account_ids())


def _spool(account):
    """Per-account spool dir; cal-main falls back to the old root files until the migration ran."""
    d = SPOOL_DIR / "accounts" / account
    if account == accounts.DEFAULT_ACCOUNT and not d.is_dir():
        return SPOOL_DIR
    return d


def _legacy_archive_present():
    try:
        return (ARCHIVE_DIR / archive.COVERAGE_NAME).exists() or any(
            archive.MONTH_NAME_RE.match(p.name) or archive.PENDING_NAME_RE.match(p.name) for p in ARCHIVE_DIR.iterdir())
    except OSError:
        return False


def _archive_dir(account):
    d = ARCHIVE_DIR / "cards" / account
    if account == accounts.DEFAULT_ACCOUNT and not d.is_dir() and _legacy_archive_present():
        return ARCHIVE_DIR
    return d


def _active_request(now=None):
    """The ONE pending/running card run (any account). A request without `account` is cal-main."""
    now = time.time() if now is None else now
    for name in ("running.json", "request.json"):
        data = _read_json(SPOOL_DIR / name)
        if data and RUN_ID_RE.fullmatch(str(data.get("run_id", ""))):
            approved = float(data.get("requested_at") or 0)
            if 0 < approved <= now + 5 and now - approved <= RUN_WINDOW:
                return dict(data, stage="running" if name == "running.json" else "queued",
                            account=str(data.get("account") or accounts.DEFAULT_ACCOUNT))
    return None


def _stopped_today(account=accounts.DEFAULT_ACCOUNT):
    last = _read_json(_spool(account) / "last-status.json") or {}
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


def _daily_limit(now=None, account=accounts.DEFAULT_ACCOUNT, default_max=None):
    """Same contract as hapoalim._daily_limit, per account: spool/accounts/<id>/runs-today.json from the runner
    is the source; a corrupt file modified today fails CLOSED; yesterday's counter is ignored (its max_runs still
    used). The optional global cap (spool/runs-total.json) also sets limit_reached (limit_scope=global)."""
    now = time.time() if now is None else now
    today = _today(now)
    path = _spool(account) / "runs-today.json"
    out = {"runs_today": 0, "max_runs": default_max or RUNNER_DEFAULT_MAX_RUNS, "max_runs_source": "default",
           "runs_state": "missing", "limit_reached": False, "resets_at": _next_midnight_iso(now)}
    if default_max:
        out["max_runs_source"] = "registry"
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
    total = _read_json(SPOOL_DIR / "runs-total.json") or {}
    cap = total.get("max_runs_total")
    if (total.get("date") == today and isinstance(cap, int) and not isinstance(cap, bool) and cap > 0
            and isinstance(total.get("runs_total"), int) and total["runs_total"] >= cap):
        out.update(limit_reached=True, limit_scope="global", runs_total=total["runs_total"], max_runs_total=cap)
    return out


def _limit_result(lim):
    out = {"status": "DAILY_LIMIT", "runs_today": lim["runs_today"], "max_runs": lim["max_runs"],
           "max_runs_source": lim["max_runs_source"], "resets_at": lim["resets_at"], "retry_today": False,
           "text": ERROR_TEXT_HE["DAILY_LIMIT"] + f" ({lim['runs_today']}/{lim['max_runs']} היום)"}
    if lim.get("limit_scope") == "global":
        out.update(limit_scope="global", runs_total=lim["runs_total"], max_runs_total=lim["max_runs_total"])
        out["text"] = ("הגעת למכסת ההתחברויות הכוללת לכל הכרטיסים להיום "
                       f"({lim['runs_total']}/{lim['max_runs_total']}). המונה מתאפס בחצות.")
    return out


def _default_max(entry):
    mx = entry.get("max_runs_per_day")
    return mx if isinstance(mx, int) and not isinstance(mx, bool) and 1 <= mx <= 100 else None


def _status_one(entry, active):
    account = entry["id"]
    last = _read_json(_spool(account) / "last-status.json") or {}
    last = {k: last[k] for k in STATUS_FIELDS if k in last}
    mine = active if active and active["account"] == account else None
    stopped = _stopped_today(account)
    lim = _daily_limit(account=account, default_max=_default_max(entry))
    act0 = ((mine or {}).get("actions") or [{}])[0] if mine else {}
    out = {
        "site": "cal",
        "last_run": last or None,
        "pending_run": {"run_id": mine["run_id"], "stage": mine["stage"],
                        "from_month": act0.get("from_month"), "to_month": act0.get("to_month")} if mine else None,
        "stopped_today": stopped,
        "can_run_today": (not stopped and not active and not lim["limit_reached"] and entry["enabled"]
                          and entry["supported"]),
        "runs_today": lim["runs_today"], "max_runs": lim["max_runs"], "max_runs_source": lim["max_runs_source"],
        "limit_reached": lim["limit_reached"], "runs_state": lim["runs_state"], "resets_at": lim["resets_at"],
    }
    if lim["limit_reached"]:
        out["error_text"] = _limit_result(lim)["text"]
    elif last.get("status") in ERROR_TEXT_HE and last.get("status") != "DAILY_LIMIT":
        out["error_text"] = ERROR_TEXT_HE[last["status"]]
    elif stopped and stopped in ERROR_TEXT_HE:
        out["error_text"] = ERROR_TEXT_HE[stopped]
    out.update(account=account, provider=entry["provider"], label=entry["label"], enabled=entry["enabled"],
               supported=entry["supported"])
    if active and not mine:
        out["busy_with_account"] = active["account"]
    if account == accounts.DEFAULT_ACCOUNT and _archive_dir(account) == ARCHIVE_DIR:
        out["migration_pending"] = True
    return out


def status(payload=None, _query=None):
    """No account: the cal-main status at the top level (the old output) + `accounts` {id: status} for all.
    With account: that account's status."""
    payload = payload or {}
    raw = str(payload.get("account") or "").strip() if isinstance(payload, dict) else ""
    active = _active_request()
    try:
        if raw:
            return _status_one(_entry(raw), active)
        reg = _registry()
        per = {a["id"]: _status_one(a, active) for a in reg["accounts"]}
        top = dict(per.get(accounts.DEFAULT_ACCOUNT) or _status_one(_entry(accounts.DEFAULT_ACCOUNT), active))
    except AccountError as exc:
        return exc.result()
    top["accounts"] = per
    if reg.get("max_runs_total"):
        top["max_runs_total"] = reg["max_runs_total"]
    return top


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


def _plan_or_none(from_m, to_m, account=accounts.DEFAULT_ACCOUNT):
    d = _archive_dir(account)
    if not d.is_dir():
        return None
    try:
        return archive.plan_gaps(d, from_m, to_m)
    except Exception:
        return None


def _runnable(account):
    entry = _entry(account)
    if not entry["supported"]:
        raise AccountError("NOT_SUPPORTED", ERROR_TEXT_HE["NOT_SUPPORTED"], account=entry["id"],
                           provider=entry["provider"])
    if not entry["enabled"]:
        raise AccountError("ACCOUNT_DISABLED", ERROR_TEXT_HE["ACCOUNT_DISABLED"], account=entry["id"])
    return entry


def propose_sync(payload, _query=None):
    payload = payload or {}
    try:
        entry = _runnable(payload.get("account"))
    except AccountError as exc:
        return exc.result()
    account = entry["id"]
    try:
        from_m, to_m, warnings = _month_range(str(payload.get("from_month") or "").strip() or None,
                                              str(payload.get("to_month") or "").strip() or None)
    except ValueError as exc:
        return {"status": "bad_request", "text": str(exc)}
    stopped = _stopped_today(account)
    if stopped:
        return {"status": "stopped_today", "account": account,
                "text": ERROR_TEXT_HE.get(stopped, ERROR_TEXT_HE["STOPPED_TODAY"])}
    active = _active_request()
    if active:
        return {"status": "already_pending", "account": account, "busy_with_account": active["account"],
                "text": "יש כבר סנכרון כרטיס מאושר שממתין או רץ (התחברות אחת בכל פעם)."}
    plan = _plan_or_none(from_m, to_m, account)
    if plan and plan.get("covered"):
        return {"status": "COVERED", "account": account, "from_month": from_m, "to_month": to_m,
                "warnings": warnings, "cards": plan.get("cards"),
                "text": (f"חודשי החיוב {from_m} עד {to_m} כבר בארכיון המקומי של כאל — בלי התחברות. "
                         "אפשר לקרוא עם card_read_month / card_read_range.")}
    lim = _daily_limit(account=account, default_max=_default_max(entry))
    if lim["limit_reached"]:
        return dict(_limit_result(lim), account=account)
    fetch_from = (plan or {}).get("fetch_from") or from_m
    fetch_to = (plan or {}).get("fetch_to") or to_m
    actions = [{"name": "card_transactions", "from_month": fetch_from, "to_month": fetch_to}]
    ident = approvals.create(ACTION, {"actions": actions, "from_month": from_m, "to_month": to_m,
                                      "fetch_from": fetch_from, "fetch_to": fetch_to, "account": account,
                                      "label": entry["label"]})
    who = "" if account == accounts.DEFAULT_ACCOUNT else f" — חשבון {entry['label']} ({account})"
    text = (f"לסנכרן עכשיו את כרטיסי האשראי של כאל{who} (חודשי חיוב {fetch_from} עד {fetch_to})? "
            "התחברות אחת מה-NUC; "
            "אם כאל תבקש קוד, יישלח אליך SMS והקוד יוזן אוטומטית פעם אחת. "
            "אחרי זה החיובים יישמרו בארכיון החודשי.")
    if warnings:
        text += " (" + "; ".join(warnings) + ")"
    return {"status": "pending_approval", "approval_id": ident, "account": account, "from_month": from_m,
            "to_month": to_m, "fetch_from": fetch_from, "fetch_to": fetch_to, "needed": (plan or {}).get("needed"),
            "warnings": warnings, "text": text}


def execute_sync(payload):
    """Approved handler: queue ONE run for the host runner. Never talks to Cal here."""
    payload = payload or {}
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions or any(
            not isinstance(a, dict) or a.get("name") != "card_transactions" for a in actions):
        raise ValueError("bad actions")
    try:
        account = _runnable(payload.get("account"))["id"]
    except AccountError as exc:
        raise ValueError(exc.code.lower())
    with _lock:
        if _stopped_today(account):
            raise ValueError("stopped_today")
        if _active_request():
            raise ValueError("already_pending")
        if _daily_limit(account=account, default_max=_default_max(_entry(account)))["limit_reached"]:
            raise ValueError("daily_limit")
        request = {"run_id": os.urandom(8).hex(), "requested_at": time.time(), "actions": actions,
                   "account": account}
        _atomic_write(SPOOL_DIR / "request.json", request)
    return {"queued": True, "run_id": request["run_id"], "account": account,
            "text": "סנכרון כאל בתור. אם כאל תשלח SMS — הקוד ייכנס אוטומטית. בדוק card_status בעוד כדקה."}


def _cards_of(account):
    d = _archive_dir(account)
    if not d.is_dir():
        return set()
    cards = set((archive.load_coverage(d).get("cards") or {}).keys())
    for p in d.glob("cal-card-*.csv"):
        m = archive.MONTH_NAME_RE.match(p.name) or archive.PENDING_NAME_RE.match(p.name)
        if m:
            cards.add(m.group(1))
    return cards


def _resolve_read(payload):
    """account and/or last4 -> account id. last4 alone resolves across accounts; ambiguity -> AMBIGUOUS_CARD."""
    raw = str(payload.get("account") or "").strip()
    card = str(payload.get("card") or "").strip() or None
    if raw:
        return _entry(raw)["id"], card
    if card:
        owners = [a for a in _account_ids() if card in _cards_of(a)]
        if len(owners) > 1:
            raise AccountError("AMBIGUOUS_CARD", ERROR_TEXT_HE["AMBIGUOUS_CARD"], card=card, accounts=owners)
        if len(owners) == 1:
            return owners[0], card
    return accounts.DEFAULT_ACCOUNT, card


def _per_account(fn, payload):
    """No account: cal-main at the top level (old output) + `accounts` {id: result} for every account."""
    raw = str((payload or {}).get("account") or "").strip() if isinstance(payload, dict) else ""
    try:
        if raw:
            return dict(fn(_entry(raw)["id"]), account=raw)
        per = {a: fn(a) for a in _account_ids()}
    except AccountError as exc:
        return exc.result()
    top = dict(per.get(accounts.DEFAULT_ACCOUNT) or fn(accounts.DEFAULT_ACCOUNT))
    top["account"] = accounts.DEFAULT_ACCOUNT
    top["accounts"] = per
    return top


def _list_one(account):
    d = _archive_dir(account)
    if not d.is_dir():
        return {"status": "no_archive", "files": [], "text": "אין ארכיון כאל עדיין."}
    report = archive.list_archive(d)
    report["text"] = f"{len(report.get('files') or [])} קבצים בארכיון כאל."
    return report


def list_archive(payload=None, _query=None):
    return _per_account(_list_one, payload if isinstance(payload, dict) else dict(_query or {}))


def read_month(payload, _query=None):
    payload = payload or {}
    ym = str(payload.get("month") or payload.get("yyyy_mm") or "").strip()
    if not ym:
        return {"status": "bad_request", "text": "month (YYYY-MM) required"}
    try:
        account, card = _resolve_read(payload)
    except AccountError as exc:
        return exc.result()
    try:
        out = archive.read_month(_archive_dir(account), ym, card=card,
                                 include_pending=bool(payload.get("include_pending")))
    except ValueError as exc:
        return {"status": "bad_request", "text": str(exc)}
    return dict(out, account=account)


def read_range(payload, _query=None):
    payload = payload or {}
    from_s = str(payload.get("from") or "").strip()
    to_s = str(payload.get("to") or "").strip()
    by = str(payload.get("by") or "charge").strip()
    if not (ISO.fullmatch(from_s) and ISO.fullmatch(to_s)):
        return {"status": "bad_request", "text": "from/to must be YYYY-MM-DD"}
    try:
        account, card = _resolve_read(payload)
    except AccountError as exc:
        return exc.result()
    try:
        out = archive.read_range(_archive_dir(account), from_s, to_s, card=card, by=by)
    except ValueError as exc:
        return {"status": "bad_request", "text": str(exc)}
    return dict(out, account=account)


def _coverage_one(account):
    d = _archive_dir(account)
    if not d.is_dir():
        return {"status": "no_archive", "text": "אין ארכיון כאל עדיין."}
    return archive.coverage_report(d)


def coverage(payload=None, _query=None):
    return _per_account(_coverage_one, payload if isinstance(payload, dict) else dict(_query or {}))


def accounts_list(_payload=None, _query=None):
    reg = _registry()
    return {"default": reg["default"], "accounts": reg["accounts"], "mirror": reg.get("mirror", "ok")}


def register_routes(router):
    router.get("/v1/cal/status", status)
    router.post("/v1/cal/status", status)
    router.post("/v1/proposals/card-sync", propose_sync, 202)
    router.get("/v1/cal/list-archive", list_archive)
    router.post("/v1/cal/list-archive", list_archive)
    router.post("/v1/cal/read-month", read_month)
    router.post("/v1/cal/read-range", read_range)
    router.get("/v1/cal/coverage", coverage)
    router.post("/v1/cal/coverage", coverage)
    router.get("/v1/cal/accounts", accounts_list)


def register_actions():
    approvals.register_action(ACTION, execute_sync)
