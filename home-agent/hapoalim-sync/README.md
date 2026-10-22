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

## Monthly archive (persistent; file names are the index)

Ben decided 2026-10-22 (10:37): no database — one CSV per account per month, kept **forever**.

| | |
|---|---|
| Dir | `~/smarthome/data/hapoalim/archive/` (0700, ben) |
| File | `hapoalim-<acct4>-YYYY-MM.csv` (0600, ben) — `acct4` = last 4 digits of the account number |
| Sidecar | `archive/_coverage.json` (0600): per account → per month `synced_from`, `synced_to`, `ranges`, `complete`, `last_sync`, `rows` |
| Lock | `archive/.lock` (flock, held for the whole merge) |
| Columns | `date, description, amount, account_balance_now, reference, account, processed_date, memo, currency, status, type, dedupe_key` |
| Retention | none (archive). `out/` keeps its 7-day cleanup; `cleanup-out.sh` never looks at `archive/`. |

**Flow.** `hapoalim_sync(from,to)` → Command Center computes the gaps from `_coverage.json` (+ always the
last `HAPOALIM_REPULL_DAYS`=3 days). No gaps → `COVERED`: no approval, no login, no SMS. Otherwise ONE
approval / ONE login / ONE fetch from `min(gap)` to `max(gap)` — the library fetches by `startDate`
through today anyway, so splitting into several fetches would only mean more logins; the action filters
to `to`. The runner re-checks the plan (defence in depth) before touching credentials.

**Merge.** The container emits an extra `archive_rows` payload (`{from, to, accounts, rows}`, full account
numbers) that the runner consumes and never writes to `out/`. Per touched month file: read → merge →
atomic rewrite (tmp + fsync + rename + dir fsync). Coverage is written **only after every month file
succeeded**; if any write fails the run reports `ARCHIVE_WRITE_FAILED`, already-rewritten months are
complete valid files, the failing month keeps its old file, and coverage is unchanged (so the next sync
re-pulls the range — the merge is idempotent).

**Dedupe** (`israeli-bank-scrapers` 6.9.0 `Transaction`: `identifier?` (אסמכתא), `date`, `processedDate`,
`chargedAmount`, `originalAmount/Currency`, `description`, `memo?`, `status: completed|pending`, `type`):
* `identifier` present → key `id:<account>:<identifier>`;
* else `sha256(account|date|processedDate|amount|description|occurrence)`, where `occurrence` numbers
  identical rows within one fetch (so two identical coffees on the same day stay two rows).
  Caveat: if the bank later returns only one of two identical id-less rows, the second stays.
* Pending → completed: same key is upserted. Pending rows inside the synced range that did not come back
  become `status=superseded` (kept for audit, hidden from `read_month`/`read_range`).

**Coverage semantics.** Coverage is per account (`acct4`) and per month, from the window the action
*actually* fetched (after the library clamp). Every account the scraper returned gets coverage for every
month in the window, even with 0 rows (quiet month: no file, `rows: 0`). A new account gets coverage only
for the window in which it first appeared. Disjoint syncs inside one month are kept as separate `ranges`
(no false coverage of the hole). A month is `complete` once one contiguous synced range spans the whole
month; the current month is therefore always open and re-merged on each sync.

**Account suffix collisions.** If two of Ben's accounts share the same last 4 digits, they share the
month files and the coverage key. Rows stay distinct (the `account` column and the dedupe key use the
full number), but coverage is merged, so a sync of one could mark the other as covered. Not expected for
a single checking account; if it happens, switch to `acct6` (one-line change in `account_suffix`).

**Backfill.** `hapoalim_sync(from=<~1 year ago>, to=<today>)`; `from` is clamped to the library cap
`today − 1 year + 1 day` and the clamp is reported in `warnings`. Existing CSVs in `out/`:
`sudo hapoalim-archive-import ~/smarthome/data/hapoalim/out/hapoalim-*.csv` (idempotent; coverage = only
the min..max of the dates of the actual rows, with a warning — the old CSVs carry no requested-range
metadata; their account column is masked `******78`, so imported rows get suffix `0078`, which differs
from the live sync's real last-4. Import only if Ben wants the 10:06 rows before the first live backfill;
after the backfill the live rows supersede by date anyway — see RUNBOOK).

**Hermes tools (read-only).** `hapoalim_list_archive`, `hapoalim_read_month(yyyy_mm, account?)`,
`hapoalim_read_range(from, to, account?)`, `hapoalim_coverage`, plus the existing `hapoalim_read_csv`.
`command-center-mcp` has no mounts; it calls the Command Center API, and the Command Center (root in its
image) gets `archive/` as a `:ro` bind mount → it can read Ben's 0600 files but cannot modify them.

**At-rest encryption (Ben to decide; not implemented).** Today the archive is plaintext, readable by
root and anyone in the `docker` group. Options: (a) put `data/hapoalim` on a LUKS/fscrypt volume unlocked
at boot (transparent; protects a stolen disk only); (b) encrypt each month file with `age` to a key in
systemd-creds and decrypt in the Command Center (protects backups too, but the CC must hold the key —
same trust domain as today); (c) SQLCipher is moot now that there is no DB. Recommendation: (a) if disk
theft / backups are the concern, otherwise leave as is.

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

# 4. rebuild/restart the services that mount the new paths (Ben's call).
#    command-center + command-center-mcp both mount the out dir / inbox;
#    hermes must restart so it reloads config + otp-relay for site=hapoalim.
docker compose up -d --build command-center command-center-mcp
docker compose restart hermes

# 5. dry check (no bank): ask Hermes "מה מצב סנכרון הבנק?" → hapoalim_status
```

### ProtectHome / ReadWritePaths

`hapoalim-run.service` sets `ProtectHome=read-only` and allows writes only under
`ReadWritePaths` (`data/hapoalim` = `out/` + `archive/`, and the OTP inbox dir). `setup-host.sh` also writes
`/etc/systemd/system/hapoalim-run.service.d/paths.conf` from `HAPOALIM_OUT_DIR` (its parent) /
`HAPOALIM_INBOX_DB`, so a custom layout stays consistent.

If you previously added a **manual** drop-in for `ReadWritePaths` (field fix for
`CSV_WRITE_FAILED`), remove it after applying this unit + `paths.conf`, then
`sudo systemctl daemon-reload`. systemd merges `ReadWritePaths` so keeping both
is not a hard conflict, but one source of truth is better.


Rollback: `bash 99-rollback-hapoalim.sh --yes` (restores the pre tag).

## Tests (box, no network)

```bash
cd home-agent/hapoalim-sync
node --test test/dates.test.mjs test/actions.test.mjs
CHROMIUM_PATH=/usr/bin/google-chrome node --test test/e2e-fake-bank.test.mjs
python3 test/runner_test.py -v
python3 test/systemd_unit_test.py -v
python3 test/archive_test.py -v
# CC: python3 tests/test_hapoalim.py -v  (from home-agent/command-center)
```

## Open risks

* Real Hapoalim login-SMS wording still lightly UNVERIFIED (PoC typed the code by hand) — phone rule +
  `OTP_HAPOALIM_REQUIRE_RE` may need a one-line tune after the first live auto run (`OTP_RELAY_DEBUG_SHAPE=1`).
* Device trust / encrypted profile is **not** kept between runs in this version (fresh tmpfs every time → SMS
  every run). Re-add profile vault later if Ben wants fewer SMSes.
* Anyone with root or `docker` on the NUC can read the CSV out dir, the **archive (kept forever)** and the sealed
  creds (by design of the host). See "At-rest encryption" above.
* Bank ToS / fraud detection on automated logins.

## fix3: full backfill, non-unique references, truthful coverage

* **Bank cap**: one transactions request returns at most the newest 150 rows. The patched scraper walks date
  windows backwards inside the one login (see `patches/README.md`), so a 1-year sync gets every row.
* **Archive key v2**: `k2:{acct4}:{sha256(reference|date|amount|description)[:32]}:{occurrence}`. References are
  not unique (a live pull had 150 rows with 42 references); `occurrence` numbers exact repeats. Old `id:`/`h:`
  keys are re-keyed on read. Rows the old key collapsed are only recoverable from out/ CSVs (kept 7 days) via
  `hapoalim-archive-repair`, or by a new sync.
* **Coverage** is recorded per account only from the first really-retrieved day: the scraper's
  `retrieval.completeFrom`, or — without metadata (old image / CSV import) — the day after the oldest row when a
  response has >= 150 rows; and never earlier than the first row if that is > 45 days after `from`.
  `hapoalim_read_month` / `hapoalim_read_range` return `complete` + `uncovered` and warn in `text`, so Hermes
  must not answer "no transactions" for days the archive does not have.
* `hapoalim-archive-repair [--dry-run]` (root, once at deploy, with Ben's approval): backup tar.gz (0600) to
  `data/hapoalim/archive-backups/`, re-key, re-add rows from out/ CSVs, drop bogus coverage.
