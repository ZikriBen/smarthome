import argparse
import shutil
import sys
from pathlib import Path

# Allow imports from the project root when running:
# python3 scripts/reclassify_existing.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.classifier import MediaType, classify_media
from app.media_paths import build_media_path


DEFAULT_MEDIA_ROOT = Path("/home/ben/media")


def human_size(size: int) -> str:
    value = float(size)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{size} B"


def unique_destination(path: Path) -> Path:
    """
    Never overwrite an existing file.

    movie.mkv
    movie (1).mkv
    movie (2).mkv
    ...
    """
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix

    counter = 1

    while True:
        candidate = (
            path.parent
            / f"{stem} ({counter}){suffix}"
        )

        if not candidate.exists():
            return candidate

        counter += 1


def classify_file(
    file_path: Path,
    media_root: Path,
) -> tuple[MediaType, Path | None, str | None]:
    result = classify_media(
        filename=file_path.name,
        caption=None,
    )

    if result.media_type == MediaType.UNKNOWN:
        return (
            MediaType.UNKNOWN,
            None,
            None,
        )

    try:
        destination = build_media_path(
            media_root=str(media_root),
            classification=result,
            original_filename=file_path.name,
        )

    except ValueError as exc:
        return (
            MediaType.UNKNOWN,
            None,
            str(exc),
        )

    if result.media_type == MediaType.TV:
        episode = result.episode

        if episode is None:
            description = result.title or "TV"
        elif episode.episode_end is not None:
            description = (
                f"{result.title} "
                f"S{(episode.season or 0):02d}"
                f"E{episode.episode_start:02d}"
                f"-E{episode.episode_end:02d}"
            )
        else:
            description = (
                f"{result.title} "
                f"S{(episode.season or 0):02d}"
                f"E{episode.episode_start:02d}"
            )

    elif result.media_type == MediaType.MOVIE:
        if result.year:
            description = (
                f"{result.title} ({result.year})"
            )
        else:
            description = (
                result.title or "Movie"
            )

    else:
        description = None

    return (
        result.media_type,
        destination,
        description,
    )


def find_incoming_files(
    incoming_dir: Path,
) -> list[Path]:
    if not incoming_dir.exists():
        return []

    files = []

    for path in incoming_dir.iterdir():
        if not path.is_file():
            continue

        # Ignore hidden/runtime files.
        if path.name.startswith("."):
            continue

        files.append(path)

    return sorted(
        files,
        key=lambda item: item.name.lower(),
    )


def migrate_file(
    source: Path,
    destination: Path,
    *,
    apply: bool,
) -> Path:
    destination = unique_destination(
        destination
    )

    if not apply:
        return destination

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Same filesystem in our current setup, so move is fast.
    # shutil.move also handles cross-filesystem cases safely.
    shutil.move(
        str(source),
        str(destination),
    )

    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reclassify existing Telegram media "
            "into Jellyfin movie/TV directories."
        )
    )

    parser.add_argument(
        "--media-root",
        default=str(DEFAULT_MEDIA_ROOT),
        help=(
            "Media root. "
            "Default: /home/ben/media"
        ),
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually move files. "
            "Without this flag, only show a dry run."
        ),
    )

    args = parser.parse_args()

    media_root = Path(
        args.media_root
    ).resolve()

    incoming_dir = (
        media_root
        / "incoming"
    )

    print()
    print("Telegram media reclassification")
    print("=" * 70)
    print(f"Media root : {media_root}")
    print(f"Incoming   : {incoming_dir}")
    print(
        "Mode       : "
        + (
            "APPLY - FILES WILL BE MOVED"
            if args.apply
            else "DRY RUN"
        )
    )
    print("=" * 70)
    print()

    files = find_incoming_files(
        incoming_dir
    )

    if not files:
        print("No files found in incoming.")
        return 0

    counts = {
        MediaType.TV: 0,
        MediaType.MOVIE: 0,
        MediaType.UNKNOWN: 0,
    }

    moved = 0
    errors = 0

    for source in files:
        try:
            (
                media_type,
                destination,
                description,
            ) = classify_file(
                source,
                media_root,
            )

            counts[media_type] += 1

            print(
                f"[{media_type.value}] "
                f"{source.name}"
            )

            print(
                f"  Size: "
                f"{human_size(source.stat().st_size)}"
            )

            if description:
                print(
                    f"  Match: {description}"
                )

            if (
                media_type
                == MediaType.UNKNOWN
                or destination is None
            ):
                print(
                    "  Action: leave in incoming"
                )
                print()
                continue

            final_destination = migrate_file(
                source,
                destination,
                apply=args.apply,
            )

            print(
                f"  -> {final_destination}"
            )

            if args.apply:
                print("  MOVED")
                moved += 1
            else:
                print("  WOULD MOVE")

            print()

        except Exception as exc:
            errors += 1

            print(
                f"[ERROR] {source.name}"
            )
            print(
                f"  {type(exc).__name__}: "
                f"{exc}"
            )
            print()

    print("=" * 70)
    print("Summary")
    print("=" * 70)

    print(
        f"TV:       "
        f"{counts[MediaType.TV]}"
    )

    print(
        f"Movies:   "
        f"{counts[MediaType.MOVIE]}"
    )

    print(
        f"Unknown:  "
        f"{counts[MediaType.UNKNOWN]}"
    )

    if args.apply:
        print(
            f"Moved:    {moved}"
        )
    else:
        print(
            "Moved:    0 (dry run)"
        )

    print(
        f"Errors:   {errors}"
    )

    if not args.apply:
        print()
        print(
            "Nothing was changed."
        )
        print(
            "Review the results and run again "
            "with --apply when ready."
        )

    return (
        1
        if errors
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
