#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_ROOT="${ROOT}/backups"
STAMP="$(date '+%Y%m%d-%H%M%S')"
DEST="${BACKUP_ROOT}/${STAMP}"

mkdir -p "${DEST}"

echo "Backing up Homarr to:"
echo "${DEST}"

docker compose \
  -f "${ROOT}/compose.yaml" \
  stop homarr

cleanup() {
  docker compose \
    -f "${ROOT}/compose.yaml" \
    start homarr >/dev/null 2>&1 || true
}

trap cleanup EXIT

cp -a "${ROOT}/appdata" "${DEST}/appdata"
cp "${ROOT}/.env" "${DEST}/.env"
cp "${ROOT}/compose.yaml" "${DEST}/compose.yaml"

if [ -f "${ROOT}/board.css" ]; then
  cp "${ROOT}/board.css" "${DEST}/board.css"
fi

echo
echo "Backup complete:"
du -sh "${DEST}"
