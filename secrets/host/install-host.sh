#!/bin/bash
# Install smarthome-secrets on the NUC host (run from the repo: sudo secrets/host/install-host.sh).
#
#   sudo secrets/host/install-host.sh              install/update files + daemon-reload ONLY
#   sudo secrets/host/install-host.sh --enable     ... and `systemctl enable` the oneshot
#   sudo secrets/host/install-host.sh --enable --start   ... and run it now
#   sudo secrets/host/install-host.sh --uninstall  remove CLI/unit/shim (keeps credstore + /etc config)
#
# Never touches .env files, containers, or credentials. DESTDIR=<dir> installs into a
# fake root (tests) and skips systemctl.
set -euo pipefail

HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)        # <repo>/secrets
REPO_ROOT_DEFAULT=$(CDPATH= cd -- "$HERE/.." && pwd)
DESTDIR="${DESTDIR:-}"
ENABLE=0; START=0; UNINSTALL=0; REPO_ROOT="$REPO_ROOT_DEFAULT"
for a in "$@"; do
  case "$a" in
    --enable) ENABLE=1 ;;
    --start) START=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --repo=*) REPO_ROOT="${a#--repo=}" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

LIB="$DESTDIR/usr/local/lib/smarthome-secrets"
SBIN="$DESTDIR/usr/local/sbin"
ETC="$DESTDIR/etc/smarthome-secrets"
CREDSTORE="$DESTDIR/etc/credstore.encrypted/smarthome"
UNITDIR="$DESTDIR/etc/systemd/system"
NAMES=(smarthome-secret smarthome-secrets smarthome-secrets-migrate smarthome-secrets-rollback)

if [ -z "$DESTDIR" ] && [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo" >&2; exit 2
fi

if [ "$UNINSTALL" = 1 ]; then
  if [ -z "$DESTDIR" ]; then
    systemctl disable smarthome-secrets.service 2>/dev/null || true
  fi
  rm -f "$UNITDIR/smarthome-secrets.service"
  for n in "${NAMES[@]}"; do rm -f "$SBIN/$n"; done
  rm -rf "$LIB"
  [ -z "$DESTDIR" ] && systemctl daemon-reload
  echo "uninstalled (kept: $CREDSTORE, $ETC, /run/smarthome-secrets until reboot)"
  echo "NOTE: containers using compose.secrets*.yaml mount $LIB/secrets-env-shim; roll those back first."
  exit 0
fi

# --- prerequisites -------------------------------------------------------------
if [ -z "$DESTDIR" ]; then
  command -v systemd-creds >/dev/null || { echo "systemd-creds not found (need systemd >= 250)" >&2; exit 2; }
  sdver=$(systemctl --version | awk 'NR==1{print $2}')
  if [ "${sdver%%.*}" -lt 250 ]; then echo "systemd $sdver < 250: no systemd-creds --with-key" >&2; exit 2; fi
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || { echo "need python3 >= 3.8" >&2; exit 2; }
  fstype=$(stat -f -c %T /run)
  [ "$fstype" = tmpfs ] || { echo "/run is $fstype, not tmpfs; refusing" >&2; exit 2; }
  if cv=$(docker compose version --short 2>/dev/null); then
    echo "docker compose $cv (overrides need >= 2.24.4 for '!reset'; migrate verifies the rendered config)"
  else
    echo "WARN: docker compose not found"
  fi
fi

# --- files ----------------------------------------------------------------------
install -d -m 0755 "$LIB" "$SBIN" "$UNITDIR"
install -d -m 0755 "$ETC"
install -d -m 0700 "$DESTDIR/etc/credstore.encrypted" "$CREDSTORE"
install -m 0755 "$HERE/bin/smarthome_secrets.py" "$LIB/smarthome_secrets.py"
install -m 0755 "$HERE/bin/secrets-env-shim" "$LIB/secrets-env-shim"
for n in "${NAMES[@]}"; do ln -sfn ../lib/smarthome-secrets/smarthome_secrets.py "$SBIN/$n"; done
install -m 0644 "$HERE/manifest.yaml" "$ETC/manifest.yaml"
# config / owners.conf are host policy: install once, never overwrite edits.
if [ ! -e "$ETC/config" ]; then
  sed "s#^REPO_ROOT=.*#REPO_ROOT=$REPO_ROOT#" "$HERE/etc/config" > "$ETC/config"
  chmod 0644 "$ETC/config"
fi
[ -e "$ETC/owners.conf" ] || install -m 0644 "$HERE/etc/owners.conf" "$ETC/owners.conf"
install -m 0644 "$HERE/systemd/smarthome-secrets.service" "$UNITDIR/smarthome-secrets.service"
if [ -z "$DESTDIR" ]; then
  chown -R root:root "$LIB" "$ETC" "$DESTDIR/etc/credstore.encrypted"
  # host key for --with-key=host (idempotent; creates /var/lib/systemd/credential.secret 0400)
  systemd-creds setup >/dev/null 2>&1 || true
  systemctl daemon-reload
  if [ "$ENABLE" = 1 ]; then systemctl enable smarthome-secrets.service; fi
  if [ "$START" = 1 ]; then systemctl start smarthome-secrets.service; fi
fi
echo "installed: $SBIN/{${NAMES[*]// /,}} $LIB/secrets-env-shim $UNITDIR/smarthome-secrets.service"
[ "$ENABLE" = 1 ] || echo "not enabled (re-run with --enable [--start])"
