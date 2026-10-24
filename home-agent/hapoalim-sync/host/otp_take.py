#!/usr/bin/env python3
"""Wait for ONE relayed Bank Hapoalim SMS code in the shared OTP inbox (the same codes.sqlite the super_hist login
uses, written by Hermes's otp-relay plugin) and print it as JSON on stdout. Started by hapoalim-runner.py as the
inbox file's OWNER (uid of hermes/otp/codes.sqlite), so root never creates journal files there.

  otp_take.py <db> <since_epoch> <timeout_s> [site]      (site: hapoalim (default) | cal)
  -> {"code": "123456"} | {"error": "OTP_TIMEOUT"} | {"error": "OTP_AMBIGUOUS"}

Only the given site (default "hapoalim"; "cal" for cal-runner.py) is read (super_hist codes are a different site and are never touched); only codes received at
or after <since> count; the read is single use: otp_inbox.take_unique deletes every hapoalim code when it finds one
(or two different ones -> AMBIGUOUS, all deleted, none used). The code is never logged.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import otp_inbox  # noqa: E402  (byte-identical copy of command-center/command_center/otp_inbox.py)

SITE = "hapoalim"
ALLOWED_SITES = ("hapoalim", "cal")   # bank/card sites only; super_hist is never readable from here
POLL = 1.0


def wait(db, since, timeout, clock=time.time, sleep=time.sleep, site=SITE):
    deadline = clock() + timeout
    while True:
        try:
            code, reason = otp_inbox.take_unique(db, site, since=since, now=clock())
        except Exception:  # a locked/broken inbox is a timeout, never a crash that could leak
            code, reason = None, "none"
        if code is not None:
            return {"code": code}
        if reason == "ambiguous":
            return {"error": "OTP_AMBIGUOUS"}
        if clock() >= deadline:
            return {"error": "OTP_TIMEOUT"}
        sleep(POLL)


def main(argv):
    try:
        db, since, timeout = argv[1], float(argv[2]), min(max(float(argv[3]), 0.0), 300.0)
        site = argv[4] if len(argv) > 4 else SITE
    except (IndexError, ValueError):
        print(json.dumps({"error": "BAD_ARGS"}))
        return 2
    if site not in ALLOWED_SITES:
        print(json.dumps({"error": "BAD_ARGS"}))
        return 2
    sys.stdout.write(json.dumps(wait(db, since, timeout, site=site)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
