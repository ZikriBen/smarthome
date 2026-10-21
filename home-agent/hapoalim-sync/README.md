# Bank Hapoalim report proof of concept

This isolated one-shot service uses `israeli-bank-scrapers` to fetch current-account
transactions and generate a private CSV report. It does not expose a port and is not
connected to Hermes.

## Safe manual test

Build and validate the image without bank credentials:

```sh
docker build -t smarthome-hapoalim-sync:test ./hapoalim-sync
docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -e CHECK_ONLY=true smarthome-hapoalim-sync:test
```

Then run the interactive helper from the `home-agent` directory:

```sh
./hapoalim-sync/run-manual.sh
```

The helper prevents credentials from being written to shell history or disk. They are
present only in the one-shot container environment while the scrape runs. A Docker
administrator could inspect them during that short window, so run this only on a trusted
host.

By default, the report covers the previous calendar month. To test a different month:

```sh
REPORT_MONTH=2026-09 ./hapoalim-sync/run-manual.sh
```

Reports are written under `hapoalim-sync/reports/` with owner-only permissions. The
`last-run.json` metadata intentionally contains no balances, full account numbers, or
transactions.

This is unofficial browser automation. Bank login changes, password expiry, additional
verification, or anti-automation controls can interrupt a run.
