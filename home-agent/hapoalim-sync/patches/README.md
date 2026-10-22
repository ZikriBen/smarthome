# Local patch of israeli-bank-scrapers 6.9.0

`israeli-bank-scrapers+6.9.0.patch` is applied after `npm ci` in the Dockerfile. It is **not** an upstream
PR and must not be pushed to eshaham/israeli-bank-scrapers.

Ideas taken from (not copied wholesale):
- [PR #1084](https://github.com/eshaham/israeli-bank-scrapers/pull/1084) — OTP form detection + `otpCodeRetriever`
  (selectors UNVERIFIED; overridable via scraper option `otpSelectors`).
- [PR #1182](https://github.com/eshaham/israeli-bank-scrapers/pull/1182) — configurable `loginTimeout`.

Differences from #1084 that matter for lockout safety:
- **one attempt only** (no 3-retry loop);
- clear `OTP_*` error names (`OTP_REQUIRED`, `OTP_TIMEOUT`, `OTP_REJECTED`, `OTP_NO_RESULT`, `OTP_FORM_CHANGED`);
- the code is never logged.

Re-generate after editing the live file under `node_modules/`:

```sh
diff -u \
  --label a/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js \
  --label b/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js \
  /tmp/hapoalim.orig.js node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js \
  > patches/israeli-bank-scrapers+6.9.0.patch
```

## Backward date-window walk (fix3)

Live finding (2026-10-22): `getAccountTransactions` sends ONE request with `numItemsPerPage=1000`, but the bank
returns at most the **newest 150** rows, so a 1-year request silently drops everything older (two 1y runs both
returned exactly 150 rows, 2026-04-15..2026-10-20).

The patch now walks backwards **inside the same login** (no extra SMS): window `[start, end]`; if it comes back
with `>= cap` rows, the next window ends **on** the oldest date returned (`retrievalEndDate` is inclusive — the
library itself sends `endDate=today` to get today's rows), so the possibly-cut boundary day is fetched again in
full. Merge = multiset on the raw key `(eventDate, referenceNumber, eventAmount, activityDescription,
serialNumber)`: per key the count is the **max** seen in any single window (the overlap day is not
double-counted; genuine identical repeats survive, because the later window has the whole day).

Stops: a window with `< cap` rows (complete back to `start`); no progress (oldest date did not move: `>= cap`
rows on one day → warning "cannot be fully retrieved", or the bank ignored the end date); `maxWindows`
(default 24 ≈ 3,600 rows). 400 ms pause between windows. Each account gets `retrieval` metadata
(`windows, rows, complete, completeFrom, stopReason, partialDays, warnings`; no amounts), which the host uses to
record archive coverage only from `completeFrom`.

Why not fixed monthly windows: always 12–13 requests even for a quiet account, and a month with > 150 rows would
still need splitting; the backward walk needs ~rows/150 requests and handles dense periods by itself.

Options (scraper option `hapoalimWalk`, set by `checking_transactions` from env): `HAPOALIM_PAGE_CAP` (150),
`HAPOALIM_WALK_MAX_WINDOWS` (24), `HAPOALIM_WALK_DELAY_MS` (400). The OTP part of the patch is unchanged.
Patched file sha256: `e4b1ef8cd51adcedcdfd0a4da164690294f733e2d302e816dc30505a46a7aa49` (checked in the Dockerfile).
