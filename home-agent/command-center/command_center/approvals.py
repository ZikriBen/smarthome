"""Audited, single-use approval workflow and action registry."""

import json
import os
import time

from .database import approval_lock, connection

_actions = {}


def register_action(name, handler):
    """Register one allowlisted execution handler during application startup."""
    if name in _actions and _actions[name] is not handler:
        raise RuntimeError(f"approval action already registered: {name}")
    _actions[name] = handler


def create(action, payload, status="pending"):
    if action not in _actions and action != "searchgram_download":
        raise ValueError("action is not allowlisted")
    ident = os.urandom(9).hex()
    with approval_lock:
        connection.execute(
            "INSERT INTO approvals VALUES (?,?,?,?,?)",
            (ident, action, json.dumps(payload), status, int(time.time())),
        )
        connection.commit()
    return ident


def approve(ident):
    """Claim before executing so concurrent button taps cannot replay an action."""
    with approval_lock:
        row = connection.execute(
            "SELECT action,payload,status,created FROM approvals WHERE id=?", (ident,)
        ).fetchone()
        if not row or row[2] != "pending":
            raise ValueError("unknown or already used approval")
        if int(time.time()) - row[3] > 900:
            connection.execute("UPDATE approvals SET status='expired' WHERE id=?", (ident,))
            connection.commit()
            raise ValueError("approval expired")
        claimed = connection.execute(
            "UPDATE approvals SET status='executing' WHERE id=? AND status='pending'", (ident,)
        ).rowcount
        connection.commit()
        if claimed != 1:
            raise ValueError("unknown or already used approval")

    handler = _actions.get(row[0])
    if handler is None:
        _set_status(ident, "failed", expected="executing")
        raise ValueError("action is not allowlisted")
    try:
        result = handler(json.loads(row[1]))
    except Exception:
        _set_status(ident, "failed", expected="executing")
        raise
    _set_status(ident, "executed", expected="executing")
    return result


def deny(ident):
    with approval_lock:
        row = connection.execute("SELECT status FROM approvals WHERE id=?", (ident,)).fetchone()
        if not row or row[0] != "pending":
            raise ValueError("unknown or already used approval")
        connection.execute("UPDATE approvals SET status='denied' WHERE id=?", (ident,))
        connection.commit()
    return {"approval_id": ident, "status": "denied"}


def _set_status(ident, status, expected=None):
    with approval_lock:
        if expected:
            connection.execute(
                "UPDATE approvals SET status=? WHERE id=? AND status=?", (status, ident, expected)
            )
        else:
            connection.execute("UPDATE approvals SET status=? WHERE id=?", (status, ident))
        connection.commit()
