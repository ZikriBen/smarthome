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
