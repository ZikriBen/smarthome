#!/bin/sh
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
image_name=${HAPOALIM_SYNC_IMAGE:-smarthome-hapoalim-sync:test}

cleanup() {
  stty echo 2>/dev/null || true
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

export HAPOALIM_USER_CODE HAPOALIM_PASSWORD
mkdir -p "$script_directory/reports"
chmod 700 "$script_directory/reports"

docker run --rm \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  --shm-size 1gb \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --mount "type=bind,source=$script_directory/reports,target=/reports" \
  -e HAPOALIM_USER_CODE \
  -e HAPOALIM_PASSWORD \
  -e REPORT_MONTH \
  "$image_name"
