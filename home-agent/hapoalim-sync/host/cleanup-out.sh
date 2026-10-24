#!/bin/bash
# Optional: delete Hapoalim CSVs older than HAPOALIM_RETENTION_DAYS (default 7) in the out dir.
# The runner already does this at the start of every run; use this for a manual / cron-less cleanup.
# Never follows symlinks. Safe to run as Ben (out dir is his) or as root.
set -euo pipefail
OUT="${HAPOALIM_OUT_DIR:-$HOME/smarthome/data/hapoalim/out}"
DAYS="${HAPOALIM_RETENTION_DAYS:-7}"
[[ "$DAYS" =~ ^[0-9]+$ ]] && (( DAYS >= 1 && DAYS <= 365 )) || { echo "bad HAPOALIM_RETENTION_DAYS" >&2; exit 2; }
[ -d "$OUT" ] || { echo "no out dir: $OUT"; exit 0; }
# Same naming as hapoalim-runner.CSV_RE
find "$OUT" -maxdepth 1 -type f -regextype posix-egrep \
  -regex '.*/hapoalim-[0-9]{8}-[0-9]{6}(-[a-z0-9_]+)*(-[0-9a-f]{4})?\.csv$' \
  -mtime +"$DAYS" -print -delete
