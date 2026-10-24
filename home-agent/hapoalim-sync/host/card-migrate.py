#!/usr/bin/env python3
"""One-time, idempotent, lossless migration of the single-account Cal layout to the multi-account layout
(cal-main namespace), and its exact reverse. Run as root by Ben (or by setup-cal-host.sh). Never prints file
contents: only names, counts and sha256 digests. Never talks to Cal.

  forward  (--apply)     archive/<cal-card-*.csv, _coverage.json>      -> archive/cards/cal-main/
                         state/<stop-*, runs-*>                       -> state/accounts/cal-main/
                         spool/<last-status.json, runs-today.json>     -> spool/accounts/cal-main/
  reverse  (--rollback)  everything the old code reads, back from the cal-main namespace to the roots
                         (including files written by syncs AFTER the migration, so no data is left behind).
                         Other accounts (e.g. cal-2) stay in archive/cards/<id>/ untouched; the old code
                         ignores that directory, nothing is deleted.

Every move is a same-filesystem rename (atomic). sha256 of every file is taken before and verified after; a
manifest (JSON: path, size, sha256, direction, time) is written to state/migrations/. A destination that already
exists with DIFFERENT bytes stops the run before anything moves (exit 3); with identical bytes the source is a
duplicate and is removed only after the check. Holds state/run.lock (the card runner's lock) for the whole run,
so no sync can write meanwhile. --dry-run prints the plan and changes nothing.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ACCOUNT = "cal-main"
ARCHIVE_RE = re.compile(r"^(?:cal-card-\d{4}-(?:\d{4}-\d{2}|pending)\.csv|_coverage\.json)$")
STATE_RE = re.compile(r"^(?:stop|runs)-\d{4}-\d{2}-\d{2}$")
SPOOL_NAMES = {"last-status.json", "runs-today.json"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def groups(archive, state, spool):
    """(label, old_dir, new_dir, matcher)."""
    return [("archive", archive, archive / "cards" / ACCOUNT, lambda n: bool(ARCHIVE_RE.match(n))),
            ("state", state, state / "accounts" / ACCOUNT, lambda n: bool(STATE_RE.match(n))),
            ("spool", spool, spool / "accounts" / ACCOUNT, lambda n: n in SPOOL_NAMES)]


def plan(archive, state, spool, reverse=False):
    moves, conflicts = [], []
    for label, old, new, match in groups(archive, state, spool):
        src_dir, dst_dir = (new, old) if reverse else (old, new)
        if not src_dir.is_dir():
            continue
        for entry in sorted(os.scandir(src_dir), key=lambda e: e.name):
            if not match(entry.name) or not entry.is_file(follow_symlinks=False):
                continue
            src, dst = Path(entry.path), dst_dir / entry.name
            digest = sha256(src)
            item = {"group": label, "src": str(src), "dst": str(dst), "size": src.stat().st_size, "sha256": digest}
            if dst.exists():
                if dst.is_file() and not dst.is_symlink() and sha256(dst) == digest:
                    item["duplicate"] = True
                else:
                    conflicts.append(item)
                    continue
            moves.append(item)
    return moves, conflicts


def ensure_dir(path, like):
    """mkdir -p with the owner/mode of `like` (the old root) for every created level."""
    st = like.stat()
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700)
        os.chmod(d, st.st_mode & 0o777)
        if os.geteuid() == 0:
            os.chown(d, st.st_uid, st.st_gid)


def execute(moves, archive, state, spool, reverse):
    roots = {"archive": archive, "state": state, "spool": spool}
    done = []
    for m in moves:
        src, dst = Path(m["src"]), Path(m["dst"])
        if sha256(src) != m["sha256"]:
            raise RuntimeError(f"{src.name}: changed during migration")
        if m.get("duplicate"):
            src.unlink()
        else:
            if not reverse:
                ensure_dir(dst.parent, roots[m["group"]])
            os.rename(src, dst)
        if sha256(dst) != m["sha256"]:
            raise RuntimeError(f"{dst.name}: checksum mismatch after move")
        done.append(m)
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the forward plan; change nothing")
    mode.add_argument("--apply", action="store_true", help="migrate to the cal-main namespace (idempotent)")
    mode.add_argument("--rollback", action="store_true", help="move cal-main back to the old layout (idempotent)")
    mode.add_argument("--rollback-dry-run", action="store_true")
    ap.add_argument("--archive", default=os.environ.get("CAL_ARCHIVE_DIR", "/home/ben/smarthome/data/cal/archive"))
    ap.add_argument("--state", default=os.environ.get("CAL_STATE_DIR", "/var/lib/cal"))
    ap.add_argument("--spool", default=os.environ.get("CAL_SPOOL_DIR", "/var/lib/cal-spool"))
    args = ap.parse_args(argv)
    archive, state, spool = Path(args.archive), Path(args.state), Path(args.spool)
    reverse = bool(args.rollback or args.rollback_dry_run)
    dry = bool(args.dry_run or args.rollback_dry_run)
    for d in (archive, state, spool):
        if not d.is_dir():
            if dry:
                continue
            print(f"missing directory: {d}", file=sys.stderr)
            return 2
    lock = None
    if state.is_dir():
        lock = open(state / "run.lock", "a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            print("LOCKED: a card sync is running; try again in a minute", file=sys.stderr)
            return 4
    try:
        moves, conflicts = plan(archive, state, spool, reverse)
        direction = "reverse" if reverse else "forward"
        for m in moves:
            print(f"{'DRY ' if dry else ''}{direction} {m['group']}: {Path(m['src']).name} "
                  f"{'(duplicate, drop source)' if m.get('duplicate') else ''}sha256={m['sha256'][:16]}")
        for c in conflicts:
            print(f"CONFLICT {c['group']}: {Path(c['dst'])} exists with different bytes", file=sys.stderr)
        if conflicts:
            print("refusing: resolve the conflicts above (nothing was moved)", file=sys.stderr)
            return 3
        if dry:
            print(f"plan: {len(moves)} file(s); nothing changed")
            return 0
        if not moves:
            if reverse:
                for _label, _old, new, _m in groups(archive, state, spool):
                    for d in (new, new.parent):
                        try:
                            d.rmdir()
                        except OSError:
                            pass
            print(f"{direction}: nothing to do (already {'old' if reverse else 'multi-account'} layout)")
            return 0
        done = execute(moves, archive, state, spool, reverse)
        if reverse:
            # Leave the old layout exactly: drop the cal-main dirs if they are now empty (lock files only).
            for _label, old, new, _m in groups(archive, state, spool):
                for leftover in (new / ".lock",):
                    try:
                        if leftover.is_file() and leftover.stat().st_size == 0:
                            leftover.unlink()
                    except OSError:
                        pass
                for d in (new, new.parent):
                    try:
                        d.rmdir()
                    except OSError:
                        pass
        mdir = state / "migrations"
        mdir.mkdir(mode=0o700, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        manifest = {"migration": "cal-multi", "direction": direction, "account": ACCOUNT, "at": time.time(),
                    "files": done}
        path = mdir / f"cal-multi-{direction}-{stamp}-{os.getpid()}.json"
        path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        print(f"{direction}: moved {len(done)} file(s), all checksums verified; manifest {path}")
        return 0
    finally:
        if lock:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
