# Design notes (see README.md for the current contract)

Status: built on the box; **not deployed**. No live bank call from the agent.

Layering (Ben, 2026-10-22): session/login (A) vs actions registry (B) — implemented in
`lib/session.mjs` + `lib/actions/`. OTP delivery reuses Super-Hist's inbox (`hermes/otp/codes.sqlite`,
site=`hapoalim`). Host runner: `host/hapoalim-runner.py` + systemd path unit. Secrets: `hapoalim-setup`
→ systemd-creds. Full detail, deploy steps, risks, and "how to add a new action": **README.md**.
Phone rule: **PHONE-SMS-FORWARDER.md**.
