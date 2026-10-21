#!/bin/sh
# Interactive helper for a ONE-SHOT scrape. Credentials and the age identity live ONLY in a temporary
# directory that is mounted into the container (never in env, never on disk after the run). Prefer the
# host runner (host/run-once.sh) for the approved Hermes flow.
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
image_name=${HAPOALIM_SYNC_IMAGE:-smarthome-hapoalim-sync:test}
secret_dir=$(mktemp -d "${TMPDIR:-/tmp}/hapoalim-secret.XXXXXX")
chmod 700 "$secret_dir"

cleanup() {
  stty echo 2>/dev/null || true
  if [ -d "$secret_dir" ]; then
    find "$secret_dir" -type f -exec shred -u {} \; 2>/dev/null \
      || find "$secret_dir" -type f -exec rm -f {} \;
    rmdir "$secret_dir" 2>/dev/null || true
  fi
  unset HAPOALIM_USER_CODE HAPOALIM_PASSWORD
}
trap cleanup EXIT INT TERM

printf 'Bank Hapoalim user code: '
IFS= read -r HAPOALIM_USER_CODE
printf 'Bank Hapoalim password (input hidden): '
stty -echo
IFS= read -r HAPOALIM_PASSWORD
stty echo
printf '\n'

printf '%s' "$HAPOALIM_USER_CODE" >"$secret_dir/user_code"
printf '%s' "$HAPOALIM_PASSWORD" >"$secret_dir/password"
chmod 600 "$secret_dir/user_code" "$secret_dir/password"
unset HAPOALIM_USER_CODE HAPOALIM_PASSWORD

# Dev/debug helper only. The key is kept OUTSIDE the mounted state dir (never next to the ciphertext it opens).
# The production flow keeps it sealed with systemd-creds (host/setup-secrets.sh) instead.
key_dir="$script_directory/.age"
if [ ! -f "$key_dir/age.key" ]; then
  mkdir -p "$key_dir" && chmod 700 "$key_dir"
  age-keygen -o "$key_dir/age.key" >/dev/null 2>&1
  chmod 600 "$key_dir/age.key"
  printf 'Created a new age identity at %s/age.key (it unlocks the browser profile).\n' "$key_dir"
fi
cp -p "$key_dir/age.key" "$secret_dir/age.key"
chmod 600 "$secret_dir/age.key"

mkdir -p "$script_directory/reports" "$script_directory/state"
chmod 700 "$script_directory/reports" "$script_directory/state"

# Manual runs still need a RUN_ID + APPROVED_AT so the OTP broker path is exercised when present.
# Without a broker mounted, an OTP challenge ends with OTP_BROKER_UNAVAILABLE (no retry).
run_id=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
approved_at=$(date +%s)

extra_mounts=""
if [ -S "${HAPOALIM_BROKER_SOCKET:-}" ]; then
  extra_mounts="--mount type=bind,source=$(dirname "$HAPOALIM_BROKER_SOCKET"),target=/otp-broker,readonly"
fi

# shellcheck disable=SC2086
docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --shm-size 1gb \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --network "${HAPOALIM_DOCKER_NETWORK:-bridge}" \
  --mount "type=bind,source=$script_directory/reports,target=/reports" \
  --mount "type=bind,source=$script_directory/state,target=/state" \
  --mount "type=bind,source=$secret_dir,target=/run/secrets/hapoalim,readonly" \
  $extra_mounts \
  -e REPORT_MONTH \
  -e REPORTS_DIRECTORY=/reports \
  -e STATE_DIRECTORY=/state \
  -e SECRET_DIRECTORY=/run/secrets/hapoalim \
  -e RUN_ID="$run_id" \
  -e APPROVED_AT="$approved_at" \
  -e LOGIN_TIMEOUT_MS="${LOGIN_TIMEOUT_MS:-180000}" \
  "$image_name"
