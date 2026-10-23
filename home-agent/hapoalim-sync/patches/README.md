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

## Cal (visaCal) month walk — `israeli-bank-scrapers+6.9.0-visa-cal.patch`

Separate patch file, separate sha checks in the Dockerfile (original `74b50dd0…0c64`, patched
`020da53f84720ee9ece2a01294b1affa4bf4eb754c6aaab7dca2486174ea0b9b`). Not upstream; do not push.

Upstream `fetchCardData` asks `getCardTransactionsDetails` once per month for ~18 months back + 1 ahead in a
fixed loop and **throws** on the first `statusCode != 1`, losing every month already fetched, and fetches all cards
in parallel. With `options.calWalk = { months:['YYYY-MM' newest first], cap, delayMs, pending }` (set only by
`lib/cal/actions/card_transactions.mjs`; without it the scraper behaves exactly like upstream):

- one request per requested **billing month**, newest first, `delayMs` (300) between months, cards one after the other;
- the walk **stops at the first failing month** (`stopReason` `error` | `blocked` (429 / "Automation detected") |
  `status` (+`stopStatusCode`) | `shape`) and keeps the months already returned;
- every row gets `calBillingMonth` (null for pending) and `calPurchaseDate` (the original `trnPurchaseDate`; upstream
  `date` is shifted by `n-1` months for installment `n`);
- each card gets `retrieval = {months:[{month, rows, capped}], requested, complete, stopMonth, stopReason,
  stopStatusCode, cap, pending: ok|failed|blocked|skipped, pendingRows, warnings}` (counts only, no amounts);
- a month with `rows >= cap` (default 1000, `CAL_MONTH_CAP`) is flagged `capped` and never recorded as complete.
  No Cal row cap/paging is known; the flag exists so a silent server-side cap can't masquerade as complete;
- no `filterOldTransactions` in walk mode (a billing month legitimately holds older purchases).
- **transport** (`calWalk.transport`; env `CAL_API_TRANSPORT`; our action defaults to `page`, and an unset
  `calWalk.transport` in the library means `node`): `node` keeps 6.9.0 behaviour (frames, pending and month calls via
  Node `fetchPost`, guarded by `lib/cal/egress.mjs` guardFetch). `page` sends those three
  POSTs from inside the logged-in dashboard page (`calPagePost`, through the browser allowlist, which already covers
  `api.cal-online.co.il`) and retries once from Node only when the page yields no HTTP response (CORS / destroyed
  context). This ports the idea of upstream PR eshaham/israeli-bank-scrapers#1193 (open, head `82d17cf`,
  `src/scrapers/visa-cal.ts` `fetchCalApi`). Our version differs: no response bytes in error messages; 429 or HTML
  ("Request Rejected") -> "Automation detected" -> `blocked`; frames stay fatal, as in 6.9.0. `retrieval.transport`
  records which transport was used.

Re-generate:

```sh
diff -u --label a/node_modules/israeli-bank-scrapers/lib/scrapers/visa-cal.js \
  --label b/node_modules/israeli-bank-scrapers/lib/scrapers/visa-cal.js \
  /tmp/visa-cal.orig.js node_modules/israeli-bank-scrapers/lib/scrapers/visa-cal.js \
  > patches/israeli-bank-scrapers+6.9.0-visa-cal.patch
```
