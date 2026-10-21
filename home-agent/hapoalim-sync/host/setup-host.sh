#!/bin/bash
# One-time NUC setup for the Bank Hapoalim report. Run by BEN, as root (sudo), only after he approved the deploy.
# Idempotent. Creates no secret (that is setup-secrets.sh) and never talks to the bank.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TAG=smarthome-hapoalim-sync:6.9.0-otp1
OTP_GID=10077

getent group hapoalim-otp >/dev/null || groupadd --system -g "$OTP_GID" hapoalim-otp
id hapoalim >/dev/null 2>&1 || useradd --system --user-group --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin hapoalim
usermod -aG hapoalim-otp hapoalim

# Data: only uid hapoalim (the container) and root. NOT ben, NOT the Hermes/agent uids.
install -d -m 0700 -o hapoalim -g hapoalim /var/lib/hapoalim /var/lib/hapoalim/state /var/lib/hapoalim/reports
# Spool: written by the Command Center (root in its container) and the root runner; nobody else may write
# request.json (that would bypass the approval).
install -d -m 0750 -o root -g root /var/lib/hapoalim-spool
# Broker socket dir: the Command Center creates broker.sock root:hapoalim-otp 0660 here.
install -d -m 0750 -o root -g hapoalim-otp /var/lib/hapoalim-otp

# The root runner must not be editable by ben/agents: copy it root-owned out of the repo.
install -d -m 0755 -o root -g root /usr/local/lib/hapoalim /etc/hapoalim
install -m 0755 -o root -g root "$HERE/run-once.sh" /usr/local/lib/hapoalim/run-once.sh

# Image: built from the reviewed checkout, then pinned BY ID (a later re-tag by someone else is ignored).
docker build --pull -t "$TAG" "$HERE/.."
image_id=$(docker image inspect --format '{{.Id}}' "$TAG")
printf 'HAPOALIM_SYNC_IMAGE=%s\n' "$image_id" >/etc/hapoalim/image.env
chmod 0644 /etc/hapoalim/image.env
docker run --rm --network none --entrypoint node "$image_id" -e \
  "const s=require('fs').readFileSync('/app/node_modules/israeli-bank-scrapers/lib/scrapers/hapoalim.js','utf8');if(!s.includes('LOCAL PATCH'))process.exit(3)" \
  || { echo "image is NOT patched" >&2; exit 1; }

# Egress: an internal network whose only exit is the allowlisting proxy.
docker build -t hapoalim-proxy:1 "$HERE/egress"
docker network inspect hapoalim-egress >/dev/null 2>&1 || docker network create --internal hapoalim-egress
if ! docker container inspect hapoalim-proxy >/dev/null 2>&1; then
  docker run -d --name hapoalim-proxy --restart unless-stopped --read-only --tmpfs /tmp \
    --cap-drop ALL --security-opt no-new-privileges:true --memory 64m --network bridge hapoalim-proxy:1
  docker network connect hapoalim-egress hapoalim-proxy
fi

install -m 0644 "$HERE/hapoalim-run.service" "$HERE/hapoalim-run.path" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hapoalim-run.path
echo "OK. Next: sudo $HERE/setup-secrets.sh, then restart command-center with the hapoalim mounts (compose.yaml)."
