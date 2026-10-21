"""Audited, single-use approval workflow and action registry.

One decision per approval, whatever the channel (Telegram button or chat reply): the first
``decide()`` claims the row atomically (``pending`` → ``executing``/``denied``) and records who
decided, how and when in ``approval_decisions``. Every later attempt gets the recorded outcome back
(``already_decided``) instead of an error, so the caller can show it and remove stale controls.
Pending approvals expire after ``TTL_SECONDS``.
"""

import json
import os
import time

from .database import approval_lock, connection

TTL_SECONDS = 900
# Rows of these actions share the table as an audit log; they are never human-approvable.
_AUDIT_ONLY = {"searchgram_download"}

_actions = {}

connection.execute(
    "CREATE TABLE IF NOT EXISTS approval_decisions (id TEXT PRIMARY KEY, decision TEXT NOT NULL, "
    "via TEXT NOT NULL, actor TEXT NOT NULL, decided_at INTEGER NOT NULL, error TEXT)"
)
connection.commit()


def register_action(name, handler):
    """Register one allowlisted execution handler during application startup."""
    if name in _actions and _actions[name] is not handler:
        raise RuntimeError(f"approval action already registered: {name}")
    _actions[name] = handler


def create(action, payload, status="pending"):
    if action not in _actions and action not in _AUDIT_ONLY:
        raise ValueError("action is not allowlisted")
    ident = os.urandom(9).hex()
    with approval_lock:
        connection.execute(
            "INSERT INTO approvals (id,action,payload,status,created) VALUES (?,?,?,?,?)",
            (ident, action, json.dumps(payload), status, int(time.time())),
        )
        connection.commit()
    return ident


# --------------------------------------------------------------------------- human summary

def _clip(text, limit=80):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _when(value):
    value = str(value or "")
    # "2026-10-23T08:00:00+03:00" → "23.10 08:00"; all-day "2026-10-23" → "23.10"
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        out = f"{value[8:10]}.{value[5:7]}"
        if len(value) >= 16 and value[10] == "T":
            out += f" {value[11:16]}"
        return out
    return value


def summarize(action, payload):
    """One short, human line describing what an approval will do (shown on the Telegram prompt)."""
    p = payload if isinstance(payload, dict) else {}
    try:
        if action == "calendar_create":
            line = f"יצירת אירוע ״{_clip(p.get('summary'), 50)}״ {_when(p.get('start'))}"
            rule = (p.get("recurrence") or {}).get("human") if isinstance(p.get("recurrence"), dict) else None
            return line + (f" ({_clip(rule, 60)})" if rule else "")
        if action == "calendar_update":
            before, after = p.get("before") or {}, p.get("after") or {}
            line = f"עדכון אירוע ״{_clip(before.get('summary'), 40)}״ {_when(before.get('start'))}"
            return line + f" → ״{_clip(after.get('summary'), 40)}״ {_when(after.get('start'))}"
        if action == "calendar_delete":
            expected = p.get("event") or p.get("expected") or {}
            what = "סדרה" if p.get("scope") == "series" else "אירוע"
            return f"מחיקת {what} ״{_clip(expected.get('summary'), 50)}״ {_when(expected.get('start'))}"
        if action == "gmail_send":
            return f"שליחת מייל ל-{_clip(p.get('to'), 50)}: ״{_clip(p.get('subject'), 50)}״"
        if action == "docker_restart":
            return f"אתחול הקונטיינר {_clip(p.get('container'), 40)}"
        if action in ("super_hist_cart_write", "super_hist_cart_batch"):
            # Built by super_hist._propose: store, item, unit price, qty before→after, expected new total.
            return _clip(p.get("summary"), 220)
        if action == "price_watch_create":
            return f"מעקב מחיר {_clip(p.get('url'), 60)} (יעד {p.get('target')})"
    except Exception:
        pass
    return action.replace("_", " ")


# --------------------------------------------------------------------------- state

def _expire_stale_locked(now):
    connection.execute(
        "UPDATE approvals SET status='expired' WHERE status='pending' AND created<?",
        (now - TTL_SECONDS,),
    )


def expire_stale():
    """Mark every pending approval past its window as expired (lazy sweep)."""
    with approval_lock:
        _expire_stale_locked(int(time.time()))
        connection.commit()


def _decision_locked(ident):
    row = connection.execute(
        "SELECT decision,via,actor,decided_at,error FROM approval_decisions WHERE id=?", (ident,)
    ).fetchone()
    if not row:
        return None
    return {"decision": row[0], "via": row[1], "actor": row[2], "decided_at": row[3], "error": row[4]}


def _view_locked(ident):
    row = connection.execute(
        "SELECT action,payload,status,created FROM approvals WHERE id=?", (ident,)
    ).fetchone()
    if not row or row[0] in _AUDIT_ONLY:
        return None
    try:
        payload = json.loads(row[1])
    except ValueError:
        payload = {}
    return {
        "approval_id": ident, "action": row[0], "status": row[2], "created": row[3],
        "expires_at": row[3] + TTL_SECONDS, "summary": summarize(row[0], payload),
        "decided": _decision_locked(ident),
    }


def get(ident):
    """Public, payload-free view of one approval (status, summary, who decided)."""
    with approval_lock:
        _expire_stale_locked(int(time.time()))
        connection.commit()
        view = _view_locked(ident)
    if view is None:
        return {"approval_id": ident, "status": "unknown"}
    return view


def decide(ident, decision, via="button", actor=""):
    """Approve or deny once. Never raises for a lost race, expiry or unknown id: returns the outcome.

    Returns ``{"approval_id", "status": executed|denied|expired|failed|executing|unknown,
    "already_decided": bool, "decided": {decision, via, actor, decided_at, error}|None,
    "summary", "result"?, "error"?}``.
    """
    if decision not in ("approve", "deny"):
        raise ValueError("decision must be approve or deny")
    via = via if via in ("button", "chat") else "button"
    actor = str(actor or "")[:64]
    now = int(time.time())
    with approval_lock:
        _expire_stale_locked(now)
        view = _view_locked(ident)
        if view is None:
            connection.commit()
            return {"approval_id": ident, "status": "unknown", "already_decided": False, "decided": None}
        if view["status"] != "pending":
            connection.commit()
            return dict(view, already_decided=view["status"] != "expired" or view["decided"] is not None)
        new_status = "executing" if decision == "approve" else "denied"
        claimed = connection.execute(
            "UPDATE approvals SET status=? WHERE id=? AND status='pending'", (new_status, ident)
        ).rowcount
        if claimed != 1:  # defensive: the lock already serialises claims
            connection.commit()
            return dict(_view_locked(ident) or view, already_decided=True)
        connection.execute(
            "INSERT OR REPLACE INTO approval_decisions (id,decision,via,actor,decided_at) VALUES (?,?,?,?,?)",
            (ident, decision, via, actor, now),
        )
        connection.commit()
        payload = json.loads(connection.execute(
            "SELECT payload FROM approvals WHERE id=?", (ident,)).fetchone()[0])

    decided = {"decision": decision, "via": via, "actor": actor, "decided_at": now, "error": None}
    base = {"approval_id": ident, "action": view["action"], "summary": view["summary"],
            "already_decided": False, "decided": decided}
    if decision == "deny":
        return dict(base, status="denied")

    handler = _actions.get(view["action"])
    if handler is None:
        _set_status(ident, "failed", expected="executing", error="action is not allowlisted")
        return dict(base, status="failed", error="action is not allowlisted")
    try:
        result = handler(payload)
    except Exception as exc:
        error = str(exc)[:500]
        _set_status(ident, "failed", expected="executing", error=error)
        return dict(base, status="failed", error=error, decided=dict(decided, error=error))
    _set_status(ident, "executed", expected="executing")
    return dict(base, status="executed", result=result)


def approve(ident, via="button", actor=""):
    """Compatibility wrapper: raises like the original API when nothing was executed."""
    outcome = decide(ident, "approve", via, actor)
    if outcome["status"] == "executed" and not outcome.get("already_decided"):
        return outcome
    if outcome["status"] == "expired" and not outcome.get("already_decided"):
        raise ValueError("approval expired")
    if outcome["status"] == "failed" and not outcome.get("already_decided"):
        raise ValueError(outcome.get("error") or "approval failed")
    raise ValueError("unknown or already used approval")


def deny(ident, via="button", actor=""):
    outcome = decide(ident, "deny", via, actor)
    if outcome["status"] == "denied" and not outcome.get("already_decided"):
        return {"approval_id": ident, "status": "denied"}
    raise ValueError("unknown or already used approval")


def _set_status(ident, status, expected=None, error=None):
    with approval_lock:
        if expected:
            connection.execute(
                "UPDATE approvals SET status=? WHERE id=? AND status=?", (status, ident, expected)
            )
        else:
            connection.execute("UPDATE approvals SET status=? WHERE id=?", (status, ident))
        if error is not None:
            connection.execute("UPDATE approval_decisions SET error=? WHERE id=?", (error, ident))
        connection.commit()
