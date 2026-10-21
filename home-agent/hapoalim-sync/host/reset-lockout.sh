#!/bin/bash
# The ONLY way to clear a lockout after an authentication failure. Ben runs it himself (sudo) after he checked
# the bank side (e.g. logged in once on the bank's own site/app). Nothing automatic ever calls this.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
STATE=/var/lib/hapoalim/state/run-state.json
[ -f "$STATE" ] || { echo "no state yet"; exit 0; }
python3 - "$STATE" <<'PY'
import json, os, sys
path = sys.argv[1]
data = json.load(open(path))
lock = data.get("lockout")
if not lock:
    print("not locked out"); sys.exit(0)
print(f"locked out: {lock.get('reason')} at {lock.get('at')}")
if input("type 'reset' to clear: ").strip() != "reset":
    sys.exit("unchanged")
data.pop("lockout", None)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
st = os.stat(path)
os.chmod(tmp, 0o600); os.chown(tmp, st.st_uid, st.st_gid); os.replace(tmp, path)
print("cleared (the daily limit still applies)")
PY
cp -p "$STATE" /var/lib/hapoalim-spool/state.json 2>/dev/null || true
