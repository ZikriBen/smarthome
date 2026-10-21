"""Remove PoC 'Watch now' entries from the streams dir and tell Jellyfin.

Dry-run by default. Only folders that carry the PoC marker (.tgstream.json)
are ever touched. Run inside the container (files are owned by its user):

    docker exec telegram-browser python -m app.stream.cleanup --all            # dry run
    docker exec telegram-browser python -m app.stream.cleanup --all --apply
    docker exec telegram-browser python -m app.stream.cleanup --variant 12345 --apply

No Telegram client is created. Jellyfin is notified via /Library/Media/Updated
(Deleted) if JELLYFIN_API_KEY_FILE or JELLYFIN_API_KEY is set, else that step is skipped.
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from app.stream import watch


def plan(root: Path, variant: int | None) -> list[tuple[Path, list[Path], bool]]:
    """[(folder, files_to_delete, delete_whole_folder)]"""
    out = []
    if not root.is_dir():
        return out
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        marker = folder / watch.MARKER
        if not marker.is_file():
            continue
        try:
            variants = json.loads(marker.read_text()).get("variants", {})
        except (OSError, ValueError):
            variants = {}
        if variant is None:
            out.append((folder, [], True))
            continue
        name = variants.get(str(variant))
        if name is None:
            continue
        if len(variants) <= 1:
            out.append((folder, [], True))
        else:
            out.append((folder, [folder / name], False))
    # stale temp folders from interrupted writes
    if variant is None:
        out += [(p, [], True) for p in root.glob(".tmp-*") if p.is_dir()]
    return out


def apply_plan(items, variant: int | None) -> None:
    for folder, files, whole in items:
        if whole:
            shutil.rmtree(folder)
        else:
            for f in files:
                f.unlink(missing_ok=True)
            marker = folder / watch.MARKER
            data = json.loads(marker.read_text())
            data["variants"].pop(str(variant), None)
            marker.write_text(json.dumps(data))


async def notify(items, root: Path) -> str:
    key = watch.jellyfin_api_key()
    if not key:
        return "jellyfin refresh skipped (no API key)"
    jf = watch.JellyfinClient(os.getenv("JELLYFIN_URL", "http://<LAN_IP>:8096"), key)
    jroot = os.getenv("JELLYFIN_STREAMS_PATH", "/media/telegram-streams")
    sent = 0
    for folder, _, _ in items:
        if folder.name.startswith("."):
            continue
        path = str(Path(jroot) / folder.name)
        try:
            await jf.report_path(path, "Deleted")
            sent += 1
        except Exception as exc:  # noqa: BLE001
            return f"jellyfin notify failed: {type(exc).__name__}"
    lib = os.getenv("JELLYFIN_STREAMS_LIBRARY_ID", "").strip()
    if lib:
        try:
            await jf.refresh_library(lib)
        except Exception as exc:  # noqa: BLE001
            return f"jellyfin library refresh failed: {type(exc).__name__}"
    return f"jellyfin notified ({sent} paths{', library refreshed' if lib else ''})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true")
    g.add_argument("--variant", type=int)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--root", default=os.getenv("TG_STREAMS_DIR", "/streams"))
    ap.add_argument("--no-jellyfin", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root)
    items = plan(root, None if args.all else args.variant)
    for folder, files, whole in items:
        what = "folder" if whole else ", ".join(f.name for f in files)
        print(f"{'DELETE' if args.apply else 'would delete'}: {folder.name} [{what}]")
    if not items:
        print("nothing to clean")
        return 0
    if not args.apply:
        print("dry run: re-run with --apply")
        return 0
    apply_plan(items, None if args.all else args.variant)
    if not args.no_jellyfin:
        print(asyncio.run(notify(items, root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
