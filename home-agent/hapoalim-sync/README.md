# Bank Hapoalim on-demand sync

One login on the NUC, then one or more registered **actions** against the authenticated session.
First live action: `checking_transactions` (עו״ש CSV). Credentials sealed with systemd-creds (Ben types
them himself). SMS OTP arrives through the same phone → Telegram forward-bot → Hermes `otp-relay` →
`hermes/otp/codes.sqlite` path that Super-Hist already uses (site=`hapoalim` only).

**Decided (Ben, 2026-10-22):**
1. CSV at `~/smarthome/data/hapoalim/out`, mode 0600, deleted after 7 days (runner cleans at each start; also `hapoalim-cleanup-out`).
2. Hermes may read the **full** transactions (via `hapoalim_read_csv`).
3. On-demand only — no schedule / timer.

**Hard rules:** no push, no live bank call, no deploy until Ben approves in his own words. One login attempt
per run; stop-for-today after auth/OTP failure; flock against concurrent runs. Never print secrets.

## Architecture

```
Ben (Telegram) ──"תעבור לי על העו״ש"──▶ Hermes
        hapoalim_sync(from?, to?) ──approval──▶ Command Center ──request.json──▶ systemd path unit
                                                                     │
                                              hapoalim-runner.py (root, LoadCredentialEncrypted)
                                                                     │ docker run --rm -i (hardened)
                                                                     ▼
                                              auto.mjs  Layer A: session.login (stdin creds, OTP file wait)
                                                        Layer B: actions registry → checking_transactions
                                                                     │
 Bank SMS ─▶ phone (Forward SMS) ─▶ @<your_sms_relay_bot> ─▶ Hermes otp-relay ─▶ codes.sqlite
                                                                     │
                                              CSV 0600 → ~/smarthome/data/hapoalim/out
                                              Hermes: hapoalim_status → hapoalim_read_csv → analyse
```

### Layer A — session (`lib/session.mjs`)
Credentials from stdin, OTP via the relayed-file wait (120 s, codes newer than `requested_at` only),
one login attempt, profile = fresh tmpfs wiped on close. Exposes `{ scraper, page, browser }` to actions.

### Layer B — actions (`lib/actions/`)
Registry: `{ name, validate(params), run(session, params) → { ok, status, files, summary, warnings } }`.
Production register: `checking_transactions` only. Stub for next work: `_example_balances.mjs` (not registered).

### Date window (checking_transactions)
- Params: `from` / `to` as YYYY-MM-DD (Asia/Jerusalem days). Default: today−45d .. today.
- Validated: ISO format, real calendar day, `from ≤ to`, no future `from`; future `to` clamped to today.
- **Cap:** `israeli-bank-scrapers` 6.9.0 `fetchAccountData` clamps start to `moment().subtract(1,'years').add(1,'day')`
  (~364 days). We clamp the same way and return a warning. The library's own `fetchData` always ends at "today";
  we filter rows by `to` after the fetch.

### bcodes.bankhapoalim.co.il
Bank-owned tag/iframe host listed in the public site's CSP next to `*.bcodes.co.il`. In the live PoC
(2026-10-22) its request **failed** and login + OTP + transactions still worked. `lib/egress.mjs` **blocks**
`bcodes.*` explicitly so every run matches the proven one (less analytics/fingerprint egress).

## OTP path / format (same as Super-Hist)

| | |
|---|---|
| Inbox file | `home-agent/hermes/otp/codes.sqlite` (host 0700 dir, file 0600) |
| Mounted | Hermes writes; Command Center / runner reads as the inbox owner |
| Schema | `otp_codes(site, code, received, ref)` |
| Site key | `hapoalim` (Super-Hist is `super_hist` — never mixed) |
| Code | 5–6 digits (configurable `OTP_HAPOALIM_CODE_DIGITS`, default `5-6`) |
| TTL | 180 s for `hapoalim` (Super-Hist keeps 600 s) |
| Take | `take_unique` — exactly one distinct code since `requested_at`, then delete all for the site |
| Timeout | 120 s (`HAPOALIM_OTP_TIMEOUT`) |
| Phone template | `#OTP hapoalim {msg}` → `@<your_sms_relay_bot>` (see `PHONE-SMS-FORWARDER.md`) |

## How to add a new action

1. Copy `lib/actions/_example_balances.mjs` → `lib/actions/<name>.mjs`.
2. Implement `validate(params)` and `run(session, params)`. Use `session.scraper.page` (logged-in puppeteer
   Page) and/or `session.scraper.fetchData()` after setting `scraper.options.*`. Return
   `{ ok, status, files:[{kind,nameHint,body}], summary, warnings }`.
3. Register it in `lib/actions/index.mjs`: `register(myAction)`.
4. Add fake-bank coverage + a unit test; extend `hapoalim_sync` / the request schema if new params are needed.
5. Do **not** register stubs in production — keep them as `_example_*.mjs`.

## Deploy (after Ben approves in his own words)

Artifacts live in `/workspace/hapoalim-auto/` (bundle + `11-apply-hapoalim.sh` + `99-rollback-hapoalim.sh`).

On the NUC, as Ben:

```bash
# 1. apply the branch (checks HEAD == 701276f, tags hapoalim-pre-<ts>, state file)
bash 11-apply-hapoalim.sh

# 2. one-time host setup + secrets (Ben types credentials; never through chat)
cd ~/smarthome/home-agent
sudo ./hapoalim-sync/host/setup-host.sh
sudo hapoalim-setup

# 3. phone rule — see PHONE-SMS-FORWARDER.md

# 4. restart command-center so it picks up the new mounts (Ben's call)
docker compose up -d command-center

# 5. dry check (no bank): ask Hermes "מה מצב סנכרון הבנק?" → hapoalim_status
```

Rollback: `bash 99-rollback-hapoalim.sh --yes` (restores the pre tag).

## Tests (box, no network)

```bash
cd home-agent/hapoalim-sync
node --test test/dates.test.mjs test/actions.test.mjs
CHROMIUM_PATH=/usr/bin/google-chrome node --test test/e2e-fake-bank.test.mjs
python3 test/runner_test.py -v
# CC: python3 tests/test_hapoalim.py -v  (from home-agent/command-center)
```

## Open risks

* Real Hapoalim login-SMS wording still lightly UNVERIFIED (PoC typed the code by hand) — phone rule +
  `OTP_HAPOALIM_REQUIRE_RE` may need a one-line tune after the first live auto run (`OTP_RELAY_DEBUG_SHAPE=1`).
* Device trust / encrypted profile is **not** kept between runs in this version (fresh tmpfs every time → SMS
  every run). Re-add profile vault later if Ben wants fewer SMSes.
* Anyone with root or `docker` on the NUC can read the CSV out dir and the sealed creds (by design of the host).
* Bank ToS / fraud detection on automated logins.
