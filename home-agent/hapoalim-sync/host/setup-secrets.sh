#!/bin/bash
# Ben runs this HIMSELF, in his own terminal on the NUC (sudo). Not via Hermes, an agent, or a pasted command
# with values in it. The values are read with echo off and piped (bash builtin printf: no argv, no env, no file)
# straight into `systemd-creds encrypt`, which seals them with the host key (+TPM2 when present) under
# /etc/credstore.encrypted (root 0600). Only hapoalim-run.service can decrypt them, into a private ramfs.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
command -v systemd-creds >/dev/null || { echo "systemd-creds missing (systemd >= 250)" >&2; exit 1; }
set +o history 2>/dev/null || true
umask 077
STORE=/etc/credstore.encrypted
install -d -m 0700 -o root -g root "$STORE"

seal() { systemd-creds encrypt --name="$1" - "$STORE/$1" >/dev/null && chmod 0600 "$STORE/$1"; }

trap 'stty echo 2>/dev/null || true; unset value' EXIT INT TERM
IFS= read -r -s -p 'Bank Hapoalim user code (hidden): ' value; echo
[ -n "$value" ] || { echo "empty" >&2; exit 1; }
printf '%s' "$value" | seal hapoalim.user_code
IFS= read -r -s -p 'Bank Hapoalim password (hidden): ' value; echo
[ -n "$value" ] || { echo "empty" >&2; exit 1; }
printf '%s' "$value" | seal hapoalim.password
unset value

# The age identity that encrypts the browser profile at rest. Generated inside the (offline) image, piped.
if [ ! -s "$STORE/hapoalim.age_key" ] || [ "${1:-}" = "--rotate-age-key" ]; then
  . /etc/hapoalim/image.env
  docker run --rm --network none --entrypoint age-keygen "$HAPOALIM_SYNC_IMAGE" 2>/dev/null | seal hapoalim.age_key
  # A new key cannot open the old profile: drop it (the next run is a "new device" and asks for one SMS).
  rm -f /var/lib/hapoalim/state/profile.tar.age
fi

# Verify WITHOUT showing anything: lengths only.
for name in hapoalim.user_code hapoalim.password hapoalim.age_key; do
  n=$(systemd-creds decrypt --name="$name" "$STORE/$name" - | wc -c)
  echo "$name: sealed, $n bytes"
done
