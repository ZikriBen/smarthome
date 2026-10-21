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
