#!/bin/sh
# Host-side runner, started by hapoalim-run.path when the Command Center drops an APPROVED request.json into
# the spool dir. Starts exactly ONE one-shot scraper container, then copies an allowlisted status back.
#
# What the container gets (nothing else):
#   /reports  rw  $HAPOALIM_HOME/reports   (CSV + last-run.json, 0700 hapoalim)
#   /state    rw  $HAPOALIM_HOME/state     (profile.tar.age + run-state.json, 0700 hapoalim)
#   /run/secrets/hapoalim  ro  a per-run tmpfs copy of the systemd credentials (0400 hapoalim), wiped after
#   /otp-broker ro  the dir holding the one-code broker socket (root:hapoalim-otp 0660)
#   network: hapoalim-egress (docker --internal) whose only way out is the allowlisting proxy
# Container stdout/stderr are discarded: the only output is the sanitized last-run.json.
set -eu

SPOOL_DIR=${HAPOALIM_SPOOL_DIR:-/var/lib/hapoalim-spool}
HOME_DIR=${HAPOALIM_HOME:-/var/lib/hapoalim}
REPORTS_DIR=$HOME_DIR/reports
STATE_DIR=$HOME_DIR/state
BROKER_DIR=${HAPOALIM_BROKER_DIR:-/var/lib/hapoalim-otp}
IMAGE=${HAPOALIM_SYNC_IMAGE:-smarthome-hapoalim-sync:6.9.0-otp1}
NETWORK=${HAPOALIM_DOCKER_NETWORK:-hapoalim-egress}
PROXY=${HAPOALIM_PROXY:-}
CREDS=${CREDENTIALS_DIRECTORY:-}

request="$SPOOL_DIR/request.json"
running="$SPOOL_DIR/running.json"
status_out="$SPOOL_DIR/status.json"
state_mirror="$SPOOL_DIR/state.json"

[ -f "$request" ] || exit 0
# Claim the request first (atomic rename) so a failed precondition can never re-trigger the path unit in a loop.
mv "$request" "$running"
fail_setup() {
  echo "$1" >&2
  printf '{"status":"failed","errorType":"SETUP_INCOMPLETE"}\n' >"$status_out"
  chmod 640 "$status_out"
  rm -f "$running"
  exit 1
}
for name in hapoalim.user_code hapoalim.password hapoalim.age_key; do
  [ -n "$CREDS" ] && [ -s "$CREDS/$name" ] || fail_setup "credential $name missing (run host/setup-secrets.sh)"
done
[ -S "$BROKER_DIR/broker.sock" ] || fail_setup "OTP broker socket missing"
uid=$(id -u hapoalim 2>/dev/null) && gid=$(id -g hapoalim) && otp_gid=$(getent group hapoalim-otp | cut -d: -f3) \
  && [ -n "$otp_gid" ] || fail_setup "user hapoalim / group hapoalim-otp missing (run host/setup-secrets.sh)"
docker image inspect "$IMAGE" >/dev/null 2>&1 || fail_setup "image $IMAGE not built"

run_id=$(sed -n 's/.*"run_id"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{16\}\)".*/\1/p' "$running" | head -1)
month=$(sed -n 's/.*"month"[[:space:]]*:[[:space:]]*"\([0-9]\{4\}-[0-9]\{2\}\)".*/\1/p' "$running" | head -1)
approved_at=$(sed -n 's/.*"approved_at"[[:space:]]*:[[:space:]]*\([0-9.][0-9.]*\).*/\1/p' "$running" | head -1)
[ -n "$run_id" ] && [ -n "$month" ] && [ -n "$approved_at" ] || fail_setup "bad request"
install -d -m 0700 -o "$uid" -g "$gid" "$HOME_DIR" "$REPORTS_DIR" "$STATE_DIR"

# Per-run copy of the secrets on tmpfs (/run), readable only by the container uid; wiped on any exit.
secret_dir=$(mktemp -d /run/hapoalim-secret.XXXXXX)
cleanup() {
  find "$secret_dir" -type f -exec shred -u {} \; 2>/dev/null || find "$secret_dir" -type f -exec rm -f {} \;
  rmdir "$secret_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
install -m 0400 -o "$uid" -g "$gid" "$CREDS/hapoalim.user_code" "$secret_dir/user_code"
install -m 0400 -o "$uid" -g "$gid" "$CREDS/hapoalim.password" "$secret_dir/password"
install -m 0400 -o "$uid" -g "$gid" "$CREDS/hapoalim.age_key" "$secret_dir/age.key"
chown "$uid:$gid" "$secret_dir" && chmod 0500 "$secret_dir"

set +e
docker run --rm \
  --name "hapoalim-sync-$run_id" \
  --user "$uid:$gid" --group-add "$otp_gid" \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m,mode=1777 \
  --shm-size 1gb \
  --memory 1500m --pids-limit 512 \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --log-driver none \
  --network "$NETWORK" \
  --mount "type=bind,source=$REPORTS_DIR,target=/reports" \
  --mount "type=bind,source=$STATE_DIR,target=/state" \
  --mount "type=bind,source=$secret_dir,target=/run/secrets/hapoalim,readonly" \
  --mount "type=bind,source=$BROKER_DIR,target=/otp-broker,readonly" \
  -e REPORT_MONTH="$month" \
  -e RUN_ID="$run_id" \
  -e APPROVED_AT="$approved_at" \
  -e HAPOALIM_PROXY="$PROXY" \
  -e LOGIN_TIMEOUT_MS="${LOGIN_TIMEOUT_MS:-180000}" \
  -e OTP_WAIT_SECONDS="${OTP_WAIT_SECONDS:-150}" \
  "$IMAGE" >/dev/null 2>&1
rc=$?
set -e
cleanup

# Mirror an ALLOWLISTED status + the lockout state into the spool (the Command Center reads only these).
if [ -f "$REPORTS_DIR/last-run.json" ]; then
  python3 - "$REPORTS_DIR/last-run.json" "$status_out" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
data = json.load(open(src))
keep = ("status", "errorType", "reportMonth", "completedAt", "startedAt", "transactionCount",
        "otpRequested", "otpUsed", "profile", "blockedHosts", "runId", "reportFile")
out = {k: data[k] for k in keep if k in data}   # never accounts, balances, or free-text errors
json.dump(out, open(dst, "w"))
PY
  chmod 640 "$status_out"
fi
if [ -f "$STATE_DIR/run-state.json" ]; then
  python3 - "$STATE_DIR/run-state.json" "$state_mirror" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
json.dump({k: data[k] for k in ("lastRunDay", "lockout", "lastResult") if k in data}, open(sys.argv[2], "w"))
PY
  chmod 640 "$state_mirror"
fi
rm -f "$running"
exit "$rc"
