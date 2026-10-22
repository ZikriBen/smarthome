#!/bin/bash
# One-time NUC setup for Bank Hapoalim sync. Run by BEN, as root (sudo), only after he approved the deploy.
# Idempotent. Creates no secret (that is hapoalim-setup) and never talks to the bank.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TAG=smarthome-hapoalim-sync:6.9.0-auto1
OUT_DIR=/home/ben/smarthome/data/hapoalim/out
BEN_UID=$(id -u ben)
BEN_GID=$(id -g ben)

# Data dirs: state/spool root-only; out dir owned by Ben (Hermes/CC read the CSVs; Ben can open them).
install -d -m 0700 -o root -g root /var/lib/hapoalim /var/lib/hapoalim-spool
install -d -m 0700 -o "$BEN_UID" -g "$BEN_GID" "$OUT_DIR"

# Root runner must not be editable by ben/agents: copy it root-owned out of the repo.
install -d -m 0755 -o root -g root /usr/local/lib/hapoalim /etc/hapoalim
install -m 0755 -o root -g root "$HERE/hapoalim-runner.py" /usr/local/lib/hapoalim/hapoalim-runner.py
install -m 0755 -o root -g root "$HERE/otp_take.py" /usr/local/lib/hapoalim/otp_take.py
install -m 0644 -o root -g root "$HERE/otp_inbox.py" /usr/local/lib/hapoalim/otp_inbox.py
install -m 0755 -o root -g root "$HERE/hapoalim-setup" /usr/local/sbin/hapoalim-setup
install -m 0755 -o root -g root "$HERE/cleanup-out.sh" /usr/local/sbin/hapoalim-cleanup-out

# Image: built from the reviewed checkout, then pinned BY ID.
docker build --pull -t "$TAG" "$HERE/.."
image_id=$(docker image inspect --format '{{.Id}}' "$TAG")
printf 'HAPOALIM_SYNC_IMAGE=%s\n' "$image_id" >/etc/hapoalim/image.env
chmod 0644 /etc/hapoalim/image.env
docker run --rm --network none --entrypoint node "$image_id" -e \
  "const s=require('fs').readFileSync('/app/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js','utf8');if(!s.includes('LOCAL PATCH'))process.exit(3)" \
  || { echo "image is NOT patched" >&2; exit 1; }

install -m 0644 "$HERE/hapoalim-run.service" "$HERE/hapoalim-run.path" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hapoalim-run.path
echo "OK. Next: sudo hapoalim-setup  (type the bank credentials), then restart command-center (compose mounts)."
