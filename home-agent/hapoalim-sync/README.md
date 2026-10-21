# Bank Hapoalim monthly report (local, one-shot, OTP via Ben's SMS relay)

`israeli-bank-scrapers` **6.9.0 + a local patch** (`patches/`, not upstream) logs in to Ben's own account from
the NUC, at most once a day and only after Ben approves the run in Telegram. If the bank asks for an SMS code
(new device / trust expired), the code reaches the scraper automatically through the existing relay
(phone Forward SMS → `@<your_sms_relay_bot>` → Hermes otp-relay → `codes.sqlite` → Command Center one-code
broker socket) and is typed **once**. The browser profile (device trust) is kept encrypted at rest so the
SMS is needed only when the bank asks (~monthly, unverified). The CSV stays on the NUC.

Read **DESIGN.md** first. Layout:

| path | what |
|---|---|
| `sync.mjs` | the one-shot scraper entrypoint (container) |
| `lib/` | broker client, run lock/lockout, profile vault (age), egress filter, redaction |
| `patches/` | `israeli-bank-scrapers+6.9.0.patch` (patch-package format) + notes |
| `host/` | NUC side: runner, systemd units, setup scripts Ben runs himself, egress proxy |
| `test/` | `node --test test/` — fake bank (no network), units, process-level guards |
| `run-manual.sh` | dev helper (prompts for credentials, one run); not the production path |

Tests: `npm ci && npx patch-package` (or `patch -p1 < patches/*.patch`), then `npm test`
(needs a local Chromium/Chrome for the fake-bank e2e; set `CHROMIUM_PATH`). Python side:
`cd ../command-center && python -m pytest tests/test_hapoalim.py`.
