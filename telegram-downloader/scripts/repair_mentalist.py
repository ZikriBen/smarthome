import sqlite3
import shutil
from pathlib import Path


DB_PATH = Path(
    "/home/ben/smarthome/"
    "telegram-downloader/data/downloader.db"
)

MEDIA_ROOT = Path(
    "/home/ben/media/tv"
)

SOURCE_DIRS = (
    MEDIA_ROOT / "המנטליסט ~" / "Season 03",
    MEDIA_ROOT / "+YSofgZNVx71hNTY0 **" / "Season 03",
)

DEST_DIR = (
    MEDIA_ROOT
    / "המנטליסט"
    / "Season 03"
)


def extract_episode(path: Path) -> int | None:
    name = path.stem

    marker = "S03E"

    if marker not in name:
        return None

    try:
        value = name.split(marker, 1)[1][:2]
        return int(value)
    except ValueError:
        return None


def destination_for(
    source: Path,
    episode: int,
) -> Path:
    return (
        DEST_DIR
        / (
            f"המנטליסט - "
            f"S03E{episode:02d}"
            f"{source.suffix}"
        )
    )


def update_database(
    old_path: Path,
    new_path: Path,
) -> None:
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute(
            """
            UPDATE download_jobs
            SET local_path = ?
            WHERE local_path = ?
            """,
            (
                str(new_path),
                str(old_path),
            ),
        )

        db.commit()

        print(
            f"  DB rows updated: "
            f"{cursor.rowcount}"
        )


def move_file(
    source: Path,
) -> None:
    episode = extract_episode(source)

    if episode is None:
        print(
            f"SKIP: cannot determine episode: "
            f"{source}"
        )
        return

    destination = destination_for(
        source,
        episode,
    )

    print()
    print(
        f"S03E{episode:02d}"
    )
    print(
        f"FROM: {source}"
    )
    print(
        f"TO:   {destination}"
    )

    if destination.exists():
        print(
            "SKIP: destination already exists"
        )
        return

    DEST_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    shutil.move(
        str(source),
        str(destination),
    )

    update_database(
        source,
        destination,
    )

    print(
        "MOVED"
    )


def remove_empty_tree(
    season_dir: Path,
) -> None:
    try:
        season_dir.rmdir()
    except OSError:
        return

    try:
        season_dir.parent.rmdir()
    except OSError:
        pass


def main() -> None:
    print(
        "Mentalist library normalization"
    )
    print("=" * 70)

    files: list[Path] = []

    for source_dir in SOURCE_DIRS:
        if not source_dir.exists():
            continue

        files.extend(
            path
            for path in source_dir.iterdir()
            if path.is_file()
        )

    files.sort(
        key=lambda path: (
            extract_episode(path) or 9999
        )
    )

    if not files:
        print(
            "No files found to repair."
        )
        return

    print(
        f"Found {len(files)} episode(s)"
    )

    for source in files:
        move_file(source)

    for source_dir in SOURCE_DIRS:
        remove_empty_tree(
            source_dir
        )

    print()
    print("=" * 70)
    print("Final library:")
    print()

    if DEST_DIR.exists():
        for path in sorted(
            DEST_DIR.iterdir(),
            key=lambda p: (
                extract_episode(p) or 9999
            ),
        ):
            if path.is_file():
                print(
                    path.name
                )

    print()
    print("Done.")


if __name__ == "__main__":
    main()
