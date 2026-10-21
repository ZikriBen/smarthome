# Bank Hapoalim report with SMS-relay OTP — design

Status: **code + mocks only.** No request to the bank has been made, no credential exists anywhere, nothing is
deployed. Everything marked ⚠️ is UNVERIFIED against the live bank.

## 1. Flow

```
Ben (Telegram) ──"דוח בנק"──▶ Hermes ──hapoalim_run_report──▶ Command Center ──approval card──▶ Ben approves
                                                                     │ execute_run(): re-check limits
                                                                     ▼
                                            /var/lib/hapoalim-spool/request.json {run_id, month, approved_at}
                                                                     │ systemd path unit
                                                                     ▼
 run-once.sh (root) ── LoadCredentialEncrypted → per-run tmpfs copy (0400 hapoalim) ──▶ docker run (one-shot)
                                                                     │   user hapoalim, read-only, cap-drop ALL
                                                                     ▼
 sync.mjs: run lock → decrypt profile into /tmp (tmpfs) → patched scraper → password → (bank shows OTP form?)
        └─ otpCodeRetriever ── broker.sock ──▶ Command Center broker ── take_unique(codes.sqlite, 'hapoalim')
                                                       ▲
 Bank SMS ─▶ Ben's phone (Forward SMS rule) ─▶ @<your_sms_relay_bot> ─▶ Hermes otp-relay (guard: LOGIN wording only)
        → code typed ONCE → homepage → transactions → CSV (NUC only) → profile re-encrypted → status (sanitized)
```

## 2. Library patch (`patches/israeli-bank-scrapers+6.9.0.patch`)

* Replaces Hapoalim's `postAction: waitForRedirect(page)` (fixed 20 s) with `hapoalimPostLogin`:
  wait up to `loginTimeout` (option, idea of upstream PR #1182; we pass 180 s) for **either** a redirect **or**
  the OTP form; if the form shows: `onOtpRequired()` hook, `credentials.otpCodeRetriever({attempt: 1})`,
  type the code once (one input, or one digit per input), real mouse click on submit, wait for the result.
* Selectors from upstream PR #1084 ⚠️: `form.auth-otp-login`, inputs inside it, `.btn-red_1`,
  error `.errors-rb .error-message`. Overridable with the scraper option `otpSelectors` without re-patching.
* **One attempt, never a retry.** Errors (thrown as `HapoalimOtpError`, surfaced as `errorMessage` prefix):
  `OTP_REQUIRED` (form but no retriever), `OTP_TIMEOUT` (no code delivered), `OTP_AMBIGUOUS`,
  `OTP_REJECTED` (bank shows an error), `OTP_NO_RESULT` (no answer / no homepage), `OTP_FORM_CHANGED`
  (input count or submit button don't match). The code is never logged; the error text never contains it.
* Pinned: lockfile `israeli-bank-scrapers@6.9.0` (sha512 integrity), `puppeteer@24.43.1`; the Dockerfile
  checks the sha256 of `lib/scrapers/hapoalim.js` before (pristine) and after (exact patch). The patch also
  applies with `patch-package@8` (verified on the box).

## 3. Code delivery: options and choice

| | A. narrow Command Center HTTP endpoint + dedicated token | **B. dedicated Unix socket (chosen)** | C. dedicated mounted file |
|---|---|---|---|
| what the scraper needs | network route to the CC + a bearer token (another secret to store/rotate) | one socket in a dir mounted `ro`, `root:hapoalim-otp 0660` | a dir the relay/CC writes a code file into |
| exposure | CC API port reachable from the scraper's network → breaks the "bank domains only" egress; token theft = can poll | no network at all; only processes with gid `hapoalim-otp` (only the scraper container) can connect | file sits on disk; race/cleanup; the writer must know when a run is armed; scraper needs write to delete |
| semantics enforced by | CC | CC (same code as A) | split between writer and reader |
| verdict | ok, but more moving parts | **least privilege, no token, no network** | weakest single-use guarantees |

Broker rules (`command_center/hapoalim.py: broker_handle`), same semantics as `super_hist` plus stricter:
* answers only for the **currently approved run** (`run_id` from `request.json`/`running.json`, approved
  < 30 min ago) → `NOT_ARMED` otherwise;
* `since` = the scraper's login start, clamped to ≥ the approval time → an older code is never used;
* exactly **one distinct** code (`otp_inbox.take_unique`) → two different codes = `OTP_AMBIGUOUS`, both deleted;
* **one take per run** (marker file) → `ALREADY_USED`; the code is deleted on use;
* waits ≤ 180 s; inbox TTL for `hapoalim` is 180 s (super_hist keeps 600 s);
* serves only the `hapoalim` site; never logs the code.

Relay guard (`hermes/plugins/otp-relay`): message must be `#OTP hapoalim <sms>`; the SMS must match the
**login** pattern (bank name AND a login word) and must **not** match the reject list (העברה, העברת, להעברת,
אישור פעולה, אישור עסקה, עסקה, תשלום, מוטב, הוראת קבע, הרשאה לחיוב, כרטיס, הלוואה, פיקדון, משיכה, ביט,
מט"ח, transfer, payment, beneficiary, transaction, approve). Both are configurable
(`OTP_HAPOALIM_REQUIRE_RE`, `OTP_HAPOALIM_REJECT_RE`, `OTP_HAPOALIM_CODE_DIGITS`, default 6); an invalid
config drops every bank message (fail closed); a custom REQUIRE can never bypass REJECT. ⚠️ The real wording
is unknown: defaults are a guess and deliberately strict (a false reject only costs an `OTP_TIMEOUT`).
Also: any other site (`super_hist`) drops a message that mentions the bank (mis-tagged rule).

## 4. Browser profile (device trust)

* On disk only `/var/lib/hapoalim/state/profile.tar.age` (age, X25519). The age identity is a systemd
  credential (sealed with the host key / TPM2), never next to the ciphertext.
* Each run: decrypt into the container's tmpfs `/tmp/hapoalim-profile` → `puppeteer.launch({userDataDir})`
  → after a **successful** run re-encrypt atomically (caches/locks excluded) → wipe the tmpfs copy.
  A failed run keeps the previous profile. The container stays `--rm` one-shot and `--read-only`.
* ⚠️ The profile = "trusted device" + maybe live session cookies: with the password it is full account
  access without Ben's phone. Rotate with `setup-secrets.sh --rotate-age-key` (drops the profile).

## 5. Secrets and isolation on the NUC

* User code + password + age key: `systemd-creds encrypt` → `/etc/credstore.encrypted/hapoalim.*` (root 0600),
  typed by Ben in `host/setup-secrets.sh` (echo off, piped through a builtin: no argv, env, history or file).
* `hapoalim-run.service` `LoadCredentialEncrypted=` → private ramfs → runner copies to a per-run
  `/run/hapoalim-secret.*` (tmpfs, dir 0500 / files 0400, uid `hapoalim`), mounted `ro`, shredded at exit.
  The scraper reads them as files (`SECRET_DIRECTORY`), never env. Not visible in `docker inspect`.
* uid `hapoalim` (system, nologin) owns `/var/lib/hapoalim/{state,reports}` 0700. Hermes' container never
  mounts these paths; the Command Center sees only the spool (request/status, sanitized) and the socket dir.
* The root runner is copied root-owned to `/usr/local/lib/hapoalim/` and the image is pinned **by id** in
  `/etc/hapoalim/image.env`, so an edit in the repo checkout (writable by ben/agents) does not change what
  root runs. Re-run `setup-host.sh` after a reviewed change.
* Never logged: container stdout/stderr discarded (`--log-driver none`, `>/dev/null`); `DEBUG` refused;
  no `storeFailureScreenShotPath`; free text redacted (secrets, 4+ digit runs, URL paths); the spool status
  is an allowlist (no accounts, balances, free-text errors). Telegram sees only status fields.
* Egress: the container's only network is `hapoalim-egress` (docker `--internal`); Chromium uses
  `--proxy-server=hapoalim-proxy:8888`, a tinyproxy that allows CONNECT :443 to `^(.+\.)?bankhapoalim\.co\.il$`
  only; in-browser request interception (lib/egress.mjs) is a second layer and records blocked hostnames.
* Lockout safety (`lib/run-lock.mjs`, in the scraper's own state dir): ≤ 1 run per Asia/Jerusalem day
  (recorded **before** touching the bank), one run at a time, and a persistent `lockout` after
  `INVALID_PASSWORD`, `CHANGE_PASSWORD`, `ACCOUNT_BLOCKED`, `OTP_REJECTED`, `OTP_NO_RESULT`,
  `OTP_FORM_CHANGED`. Only `host/reset-lockout.sh` (Ben, sudo) clears it. The CC refuses proposals early.

## 6. Hermes integration (minimal)

* `hapoalim_status` (read-only) and `hapoalim_run_report(month)` → approval card every run
  (`approvals` action `hapoalim_run_report`). No routine, no auto-retry. SOUL text: `../docs/SOUL_HAPOALIM_SNIPPET.md`.

## 7. Residual risks

* Anyone with root or docker-group access on the NUC (incl. an agent shell that has it) can read everything.
  If ben/agents are in the `docker` group, the uid separation is only against accidents. ⚠️ check `id ben`.
* The Command Center already has `docker.sock` mounted: its compromise is host-root regardless of this feature.
* Telegram bot messages (the forwarded login SMS) are stored on Telegram's servers (not E2E).
* A bank web session is not read-only; a compromised dependency could act within the session
  (financial actions likely need another OTP, which the relay refuses ⚠️).
* DNS from the internal network may still resolve external names (small exfil channel) ⚠️.
* Bank ToS / fraud-detection on automated logins ⚠️.
