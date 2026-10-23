#!/usr/bin/env python3
"""Fake Cal container for cal_runner_test.py (synthetic). argv[1] = behaviour, argv[2] = login counter file."""
import json
import sys

behaviour = sys.argv[1] if len(sys.argv) > 1 else "ok"
creds = json.loads(sys.stdin.readline())
if len(sys.argv) > 2:
    # One line per login; the synthetic username tells the multi-account tests whose credentials were used.
    open(sys.argv[2], "a").write(f"1 {creds.get('username', '')}\n")
# Synthetic second account (multi-account tests): different cards, 1111 shared on purpose (ambiguous last-4).
CARDS = ("3333", "1111") if creds.get("username") == "cal2user" else ("1111", "2222")
assert set(creds) >= {"username", "password"} and "userCode" not in creds
actions = creds.get("actions") or [{"name": "card_transactions"}]
a = actions[0]
from_m, to_m = a.get("from_month") or "2026-08", a.get("to_month") or "2026-11"


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def months_desc(f, t):
    out, y, m = [], int(t[:4]), int(t[5:])
    while f"{y:04d}-{m:02d}" >= f:
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


otp_req = otp_del = False
if behaviour in ("otp", "otp-timeout"):
    otp_req = True
    emit({"event": "otp_needed"})
    reply = json.loads(sys.stdin.readline())
    if not reply.get("otp"):
        emit({"event": "result", "report": {"site": "cal", "status": reply.get("error") or "OTP_TIMEOUT",
              "stage": "otp-requested", "stopForToday": True, "otpRequested": True}, "files": []})
        sys.exit(1)
    otp_del = True
if behaviour in ("cal-blocked", "captcha"):   # login-level / first-request blocks (multi-account provider stop)
    emit({"event": "result", "report": {"site": "cal", "status": "CAL_BLOCKED" if behaviour == "cal-blocked"
          else "CAPTCHA_OR_BLOCK", "stage": "transactions", "stopForToday": True, "blocked": True}, "files": []})
    sys.exit(1)
if behaviour == "invalid":
    emit({"event": "result", "report": {"site": "cal", "status": "INVALID_PASSWORD", "stage": "credentials",
          "stopForToday": True, "detail": "x"}, "files": []})
    sys.exit(1)

months = months_desc(from_m, to_m)
stop = None
if behaviour == "blocked":
    stop = months[1] if len(months) > 1 else None
retr, rows = {}, []
for card in CARDS:
    got = months if stop is None else months[:months.index(stop)]
    retr[card] = {"known": True, "months": [{"month": m, "rows": 2, "capped": False} for m in got],
                  "complete": stop is None, "stopMonth": stop, "stopReason": "blocked" if stop else None,
                  "pending": "ok", "pendingRows": 1 if card == "1111" else 0, "warnings": []}
    for m in got:
        for n in (1, 2):
            rows.append({"card": card, "billing_month": m, "purchase_date": f"{m}-0{n}", "charge_date": f"{m}-10",
                         "description": f"synthetic {n}", "amount": -n, "currency": "ILS", "status": "completed",
                         "identifier": f"{card}-{m}-{n}"})
    if card == "1111":
        rows.append({"card": card, "billing_month": None, "purchase_date": "2026-10-20", "charge_date": None,
                     "description": "pending synthetic", "amount": -3, "currency": "ILS", "status": "pending"})
payload = {"from_month": from_m, "to_month": to_m, "months_requested": months, "cards": list(CARDS),
           "retrieval": retr, "rows": rows}
report = {"site": "cal", "status": "DONE", "stage": "done", "stopForToday": behaviour == "blocked",
          "blocked": behaviour == "blocked", "otpRequested": otp_req, "otpDelivered": otp_del, "cardCount": 2,
          "transactionCount": len([r for r in rows if r["status"] == "completed"]), "from_month": from_m,
          "to_month": to_m, "secretField": "must-not-leak",
          "actions": [{"name": "card_transactions", "ok": True, "status": "DONE", "detail": "drop me"}]}
emit({"event": "result", "report": report, "files": [
    {"kind": "csv", "nameHint": "card_transactions", "body": "card,amount\n1111,-1\n"},
    {"kind": "archive_rows", "nameHint": "archive", "body": json.dumps(payload)}]})
