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

## Cal (visaCal) credit cards — option B

Same image, separate everything else. `cal-auto.mjs` (entrypoint override `--entrypoint dumb-init IMAGE -- node
/app/cal-auto.mjs`) → `lib/cal/session.mjs` (ONE login, library `visaCal` scraper) → its own action registry
(`lib/cal/actions/`, only `card_transactions`) → line protocol identical to the bank's (`username`/`password`
instead of `userCode`). Root runner `host/cal-runner.py` subclasses the Hapoalim runner (hooks: `NAME`,
`CSV_PATTERN`, `STOP`, `FIELDS`, `OTP_SITE`, `credential_payload`, `plan_actions`; defaults = Hapoalim behaviour).

| | Hapoalim | Cal |
|---|---|---|
| unit | `hapoalim-run.service/.path` | `cal-run.service/.path` (same hardening) |
| secrets | `hapoalim-setup` → `hapoalim.user_code`, `hapoalim.password` | `cal-setup` → `cal.username`, `cal.password` |
| spool / state | `/var/lib/hapoalim-spool`, `/var/lib/hapoalim` | `/var/lib/cal-spool`, `/var/lib/cal` |
| data | `~/smarthome/data/hapoalim/{out,archive}` | `~/smarthome/data/cal/{out,archive}` (dirs 0700, files 0600) |
| image pin | `/etc/hapoalim/image.env` | `/etc/cal/image.env` (`CAL_SYNC_IMAGE`, own build `6.9.0-cal1`) |
| daily limit | `HAPOALIM_MAX_RUNS_PER_DAY` 3 | `CAL_MAX_RUNS_PER_DAY` **2** (drop-in `cal-run.service.d/limits.conf`) |
| OTP site | `hapoalim` | `cal` (only if Cal shows a code form; `CAL_OTP_ENTRY=0` → `OTP_REQUIRED` stop) |
| approval | `hapoalim_sync` | `card_sync` |

**Egress.** Browser: `*.cal-online.co.il` only (+ `CAL_EXTRA_HOSTS`); everything else is aborted and its hostname
recorded (`blockedHosts`). Node side: the library calls the Cal API with Node's global `fetch` (not from the page),
so `lib/cal/egress.mjs` wraps `globalThis.fetch` for the session: only `https://api.cal-online.co.il`
(`apiHosts` / `blockedApiHosts` in the report); a 429 becomes the library's "Automation detected" error → walk
`stopReason: blocked` → `CAL_BLOCKED…`, stop for today (rows already returned are still archived). An HTML answer
from the API host (Akamai "Request Rejected") is treated the same way, with only the status recorded (`statuses.html`)
and the body never read.

**API transport (`CAL_API_TRANSPORT`, default `page`).** `page` is a port of upstream PR
eshaham/israeli-bank-scrapers#1193 (open, head `82d17cf`; `src/scrapers/visa-cal.ts` `fetchCalApi`). It sends
Frames / getClearanceRequests / getCardTransactionsDetails from inside the logged-in dashboard page, so they go out
the way Cal's own web app sends them:
- the real Chromium TLS/HTTP2 fingerprint;
- the session and Akamai sensor cookies (`credentials: 'include'`);
- the browser-set `Origin`/`Referer`/`Sec-Fetch-*`, instead of Node's undici with spoofed headers.

That traffic goes through the browser allowlist above (`*.cal-online.co.il` already covers `api.cal-online.co.il`).
If the page gets no HTTP response at all (CORS / destroyed context), it retries that request once from Node, through
the Node guard. Any HTTP answer from the page is final: 429/HTML → blocked, other non-2xx → the walk stops.

`node` is the library's 6.9.0 path: Node `fetch` from the same container and IP. Switch back with a root drop-in:
`[Service]` and `Environment=CAL_API_TRANSPORT=node` in `/etc/systemd/system/cal-run.service.d/limits.conf`, then
`daemon-reload`. No rebuild is needed; the patched library carries both paths.
Hosts the library is known to touch: `www.cal-online.co.il` (login page), `connect.cal-online.co.il` (login iframe
+ SSO), `digital-web.cal-online.co.il` (dashboard; also the API `Origin`), `api.cal-online.co.il` (Frames,
transactionsDetails, approvals/getClearanceRequests).

**Login.** One attempt. Invalid password = the iframe shows `div.general-error > div` = "שם המשתמש או הסיסמה שהוזנו
שגויים" → `INVALID_PASSWORD` (stop). Change-password form → `CHANGE_PASSWORD`. Captcha markers → `CAPTCHA_OR_BLOCK`.
Anything else after submit → `LOGIN_UNKNOWN` (stop); before submit → `LOGIN_PAGE_FAILED` (no stop).
`CAL_NAV_TIMEOUT_MS` (60 s) bounds the post-submit navigation wait. OTP (library has no hook for visaCal): a
watcher looks for a code form in the `connect.*` iframe after submit (one code input or 4–8 single-char boxes,
username field gone), emits `otp_needed` once, types the code once, clicks once, waits
`CAL_OTP_RESULT_TIMEOUT_MS` (45 s) → `OTP_REJECTED` / `OTP_NO_RESULT` / `OTP_FORM_CHANGED` / `OTP_TIMEOUT`, all
stop-for-today. The real Cal code form is UNVERIFIED (the fake mirrors a plausible one).

**Months + walk.** Patch `patches/israeli-bank-scrapers+6.9.0-visa-cal.patch` (see patches/README.md): opt-in
`calWalk` = one `getCardTransactionsDetails` request per billing month, newest → oldest, cards sequential, stop
at the first failing month (no throw), per-month `{rows, capped}` metadata. Default request = the last
`CAL_MONTHS` (3) statements + the next one; max 18 months back (library default lookback). The runner narrows it
to the months the archive still needs (`plan_gaps`): COVERED (no login, not counted) when every month is
complete + final for every card Cal last listed.

**Date semantics.** File month = **billing (statement) month** = the month the walk asked for. `purchase_date` =
original purchase day (`trnPurchaseDate`; the library's own `date` is shifted by n−1 months for installment n,
so it is NOT used). `charge_date` = debit day (`debCrdDate`). Pending rows (`getClearanceRequests`) have no
billing month/charge date → `cal-card-<last4>-pending.csv`, replaced each sync. `amount` keeps the library sign
(negative = charge). A month is `final` only after month end + `CAL_REPULL_DAYS` (10); until then it is replaced by
each new snapshot; a final month is merged by key (union). `card_read_range(by="charge")` filters on
charge_date in the billing months spanning the range; `by="purchase"` scans up to 2 later statements.

**Archive** `host/cal_archive.py` (byte-identical copy in `command-center/command_center/cal_archive.py`): see
its docstring for the `c1:` key (last4 | billing_month | identifier | purchase_date | amount | original_amount |
currency | description | installment n/m, + occurrence) and the re-key-tolerant reader.

### Cal tests (box, synthetic, no network)

```bash
cd home-agent/hapoalim-sync
node --test test/cal-units.test.mjs
CHROMIUM_PATH=/usr/bin/google-chrome node --test --test-concurrency=1 test/cal-e2e.test.mjs
python3 test/cal_runner_test.py -v && python3 test/cal_archive_test.py -v && python3 test/cal_systemd_unit_test.py -v
# CC: python3 tests/test_cal.py -v && python3 tests/test_otp_relay_cal.py -v  (from home-agent/command-center)
```

### Cal risks / unverified

* Cal's login flow and any code form are taken from library source only (no live run): the OTP form detector is a
  guess; `CAL_OTP_ENTRY=0` makes any prompt a clean `OTP_REQUIRED` stop instead.
* No known per-month row cap; `CAL_MONTH_CAP` (1000) only flags suspicious months as not complete.
* Two cards with the same last-4 share archive files (key still differs by identifier).
* Akamai bot protection on `api.cal-online.co.il` (upstream PR #1193, issue #1152): untested from a box. A block
  shows up as `CAL_BLOCKED` (429 or HTML) and the run stops for the day. Default transport is `page` (#1193 port);
  `CAL_API_TRANSPORT=node` is the 6.9.0 path. Neither has been tried against the live site.
  Library fixes after 6.9.0 that are NOT in our pin: #1153 (bank- vs. Cal-issued frame/balance selection). It affects
  only `balance`/`cardFrame`/`cardType`, which our archive doesn't use.
* The browser allowlist is `*.cal-online.co.il` only. If Cal's dashboard starts loading code from another host (a CDN),
  the SPA won't boot (`init` never appears, the same symptom as issue #1152) and `blockedHosts` names the host. Add it
  via `CAL_EXTRA_HOSTS` after checking.
* The library sends a constant `X-Site-Id`; if Cal rotates it, the walk fails with `stopReason: status` and nothing
  is marked complete (no silent gap).

## Multiple card accounts (cal-main, cal-2, …) — one contract

Same six `card_*` tools; each takes an optional `account` (`^[a-z0-9][a-z0-9-]{1,23}$`). Empty = `cal-main` (the
pre-multi account): calls without `account` return the old output unchanged (new keys only: `account`,
`accounts`, `provider`, `label`, `enabled`, `supported`, `busy_with_account`, `migration_pending`).
`card_status` / `card_coverage` / `card_list_archive` without `account` = cal-main at the top level plus
`accounts: {id: …}` for every account. `card_read_month` / `card_read_range`: `account` and/or `card` (last-4);
last-4 alone resolves across accounts, `AMBIGUOUS_CARD` (+ candidate `accounts`) if two have it. `card_sync`
proposes ONE account (default cal-main), approval card every time.

| what | where |
|---|---|
| registry (root, not secret) | `/etc/card-accounts.json` `{id, provider, label, enabled, max_runs_per_day}` + `max_runs_total`; missing = cal-main only |
| mirror for the CC | `/var/lib/cal-spool/accounts.json` (written by the runner / card-setup / setup-cal-host.sh) |
| provider adapters | `host/card_accounts.py` `PROVIDERS` (login fields, OTP site, egress hosts, archive prefix, entry). Only `cal`; anything else → `NOT_SUPPORTED` ("not supported yet") |
| credentials | `/etc/credstore.encrypted/card-<id>.<field>`; cal-main also reads the legacy `cal.username` / `cal.password` |
| unit credentials | generated drop-in `cal-run.service.d/creds.conf` (only files that exist) |
| archive | `data/cal/archive/cards/<id>/cal-card-<last4>-YYYY-MM.csv` + `_coverage.json` (same file format) |
| state | `/var/lib/cal/accounts/<id>/stop-*`, `runs-*` |
| spool | `/var/lib/cal-spool/accounts/<id>/last-status.json`, `runs-today.json`; `runs-total.json`; `status-<run>.json` |

Budget: per account `max_runs_per_day` (default `CAL_MAX_RUNS_PER_DAY` = 2); optional global cap
`CARD_MAX_RUNS_TOTAL` (unit env) / `max_runs_total` (registry), lower wins, 0 = off. COVERED never counts.
Stop-for-today after an auth/OTP failure is per account. A bot-detection / 429 / HTML-instead-of-JSON / captcha
block (`CAL_BLOCKED`, `CAPTCHA_OR_BLOCK`, or a report with `blocked: true`) on ANY account sets a provider-wide stop:
`/var/lib/cal/providers/cal/stop-today.json` (root, authoritative) mirrored to
`/var/lib/cal-spool/providers/cal/stop-today.json` `{date, provider, status, reason, triggered_by, at}`; first
trigger of the Israel day wins, expires at the date rollover. The CC refuses `card_sync` for every account of the
provider with `PROVIDER_STOPPED_TODAY` (+ `triggered_by`, `reason`, `at_il`) before an approval/spool; the runner
re-checks before login (never counted). Status: `provider_stopped_today` per account, `provider_stops` at the top.
Code without this feature ignores the files. One login at a time: one spool request for all
accounts, the card lock `/var/lib/cal/run.lock` AND the Hapoalim runner's lock `/var/lib/hapoalim/run.lock`
(`CARD_BANK_LOCK`), both non-blocking → `LOCKED` (either side). Service name stays `cal-run.service/.path`.

Setup: `sudo card-setup <account> [--provider cal] [--label TEXT] [--max-runs N]` asks only that provider's login
fields. `sudo cal-setup` = `card-setup cal-main --provider cal`. `sudo card-setup cal-main --from-legacy`
optionally re-seals the old `cal.*` under the new names (decrypt→encrypt in one pipe, verified, legacy kept).

Migration: `sudo card-migrate --dry-run | --apply | --rollback-dry-run | --rollback` (idempotent, same-fs renames,
sha256 before/after, manifest in `/var/lib/cal/migrations/`, conflict = nothing moves). `setup-cal-host.sh` runs
`--apply`. While the old layout exists the runner refuses with `MIGRATION_PENDING` (no login) and the CC reads
cal-main from the old layout. Rollback moves cal-main (including syncs made after the migration) back; other
accounts stay in `archive/cards/<id>/` untouched.

Tests: `python3 test/card_multi_test.py` (host) and `python3 tests/test_cal_multi.py` (CC).
