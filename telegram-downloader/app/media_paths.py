from pathlib import Path

from app.classifier import ClassificationResult, MediaType


def sanitize_path_component(value: str) -> str:
    value = value.strip()

    for char in ("/", "\\", "\0"):
        value = value.replace(char, "_")

    return value.strip(" .")


def build_tv_path(
    *,
    media_root: str,
    classification: ClassificationResult,
    original_filename: str,
) -> Path:
    if classification.media_type != MediaType.TV:
        raise ValueError("classification is not TV")

    if classification.episode is None:
        raise ValueError("TV classification has no episode")

    if not classification.title:
        raise ValueError("TV classification has no title")

    episode = classification.episode
    title = sanitize_path_component(
        classification.title
    )

    season = episode.season or 0

    extension = Path(
        original_filename
    ).suffix

    season_dir = f"Season {season:02d}"

    if episode.episode_end is not None:
        episode_part = (
            f"S{season:02d}"
            f"E{episode.episode_start:02d}"
            f"-E{episode.episode_end:02d}"
        )
    else:
        episode_part = (
            f"S{season:02d}"
            f"E{episode.episode_start:02d}"
        )

    filename = (
        f"{title} - "
        f"{episode_part}"
        f"{extension}"
    )

    return (
        Path(media_root)
        / "tv"
        / title
        / season_dir
        / filename
    )


def build_movie_path(
    *,
    media_root: str,
    classification: ClassificationResult,
    original_filename: str,
) -> Path:
    if classification.media_type != MediaType.MOVIE:
        raise ValueError("classification is not MOVIE")

    if not classification.title:
        raise ValueError("movie classification has no title")

    title = sanitize_path_component(
        classification.title
    )

    year = classification.year

    extension = Path(
        original_filename
    ).suffix

    if year is not None:
        folder_name = f"{title} ({year})"
        filename = f"{title} ({year}){extension}"
    else:
        folder_name = title
        filename = f"{title}{extension}"

    return (
        Path(media_root)
        / "movies"
        / folder_name
        / filename
    )


def build_media_path(
    *,
    media_root: str,
    classification: ClassificationResult,
    original_filename: str,
) -> Path:
    if classification.media_type == MediaType.TV:
        return build_tv_path(
            media_root=media_root,
            classification=classification,
            original_filename=original_filename,
        )

    if classification.media_type == MediaType.MOVIE:
        return build_movie_path(
            media_root=media_root,
            classification=classification,
            original_filename=original_filename,
        )

    raise ValueError(
        f"unsupported media type: {classification.media_type}"
    )
