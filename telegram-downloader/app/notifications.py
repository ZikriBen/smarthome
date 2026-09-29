def format_size(size: int | None) -> str:
    if size is None:
        return "Unknown size"

    mb = size / 1024 / 1024

    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"

    return f"{mb:.1f} MB"


def accepted_message(
    filename: str | None,
    file_size: int | None,
) -> str:
    return (
        "📥 Accepted\n\n"
        f"File: {filename or 'Telegram media'}\n"
        f"Size: {format_size(file_size)}\n\n"
        "Added to the download queue."
    )


def available_message(
    filename: str,
    duration_seconds: float,
) -> str:
    minutes = int(duration_seconds // 60)
    seconds = int(duration_seconds % 60)

    if minutes:
        duration = f"{minutes}m {seconds}s"
    else:
        duration = f"{seconds}s"

    return (
        "✅ Available\n\n"
        f"{filename}\n\n"
        "Ready to watch in Jellyfin.\n"
        f"Download time: {duration}"
    )


def failed_message(
    filename: str | None,
    attempts: int,
) -> str:
    return (
        "❌ Download failed\n\n"
        f"{filename or 'Telegram media'}\n\n"
        f"Failed after {attempts} attempts."
    )
