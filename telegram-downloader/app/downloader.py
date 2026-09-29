import asyncio
import os
import re
import shutil
import time
from pathlib import Path

from telethon import TelegramClient

from app.classifier import (
    MediaType,
    classify_media,
)
from app.config import Config
from app.database import Database
from app.jellyfin import JellyfinClient
from app.media_paths import build_media_path
from app.models import JobRecord
from app.notifications import (
    available_message,
    failed_message,
)


REQUEST_SIZE = 512 * 1024


def safe_filename(
    filename: str | None,
    job_id: int,
) -> str:
    if not filename:
        return f"telegram-{job_id}.bin"

    filename = Path(filename).name

    return re.sub(
        r"[\x00-\x1f/\\\\]",
        "_",
        filename,
    )


class ProgressReporter:
    def __init__(
        self,
        job: JobRecord,
        initial_bytes: int = 0,
        interval: float = 60.0,
    ):
        self.job = job
        self.initial_bytes = initial_bytes
        self.interval = interval
        self.started = time.monotonic()
        self.last_print = 0.0

    def report(
        self,
        current: int,
        total: int,
    ) -> None:
        now = time.monotonic()

        if (
            now - self.last_print
            < self.interval
            and current != total
        ):
            return

        self.last_print = now

        elapsed = max(
            now - self.started,
            0.001,
        )

        transferred = max(
            current - self.initial_bytes,
            0,
        )

        speed_mb_s = (
            transferred
            / 1024
            / 1024
            / elapsed
        )

        current_mb = (
            current / 1024 / 1024
        )

        total_mb = (
            total / 1024 / 1024
        )

        percent = (
            current / total * 100
            if total
            else 0
        )

        print(
            f"[job {self.job.id}] "
            f"{current_mb:.1f}/"
            f"{total_mb:.1f} MB "
            f"({percent:.1f}%) "
            f"{speed_mb_s:.1f} MB/s",
            flush=True,
        )


class Downloader:
    def __init__(
        self,
        config: Config,
        database: Database,
        client: TelegramClient,
    ):
        self.config = config
        self.database = database
        self.client = client

        self.jellyfin = JellyfinClient(
            config
        )

        self.media_root = Path(
            config.media_root
        )

        self.incoming_dir = (
            self.media_root
            / "incoming"
        )

        self.tv_dir = (
            self.media_root
            / "tv"
        )

        self.movies_dir = (
            self.media_root
            / "movies"
        )

        self.part_dir = (
            self.incoming_dir
            / ".part"
        )

        for directory in (
            self.incoming_dir,
            self.tv_dir,
            self.movies_dir,
            self.part_dir,
        ):
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )

    def get_free_disk_gb(self) -> float:
        usage = shutil.disk_usage(
            self.config.media_root
        )

        return (
            usage.free
            / 1024
            / 1024
            / 1024
        )

    def has_enough_disk_space(self) -> bool:
        return (
            self.get_free_disk_gb()
            >= self.config.min_free_disk_gb
        )

    def get_final_path(
        self,
        job: JobRecord,
        filename: str,
    ) -> Path:
        classification = classify_media(
            filename=job.original_filename,
            caption=job.caption,
        )

        if (
            classification.media_type
            in (
                MediaType.TV,
                MediaType.MOVIE,
            )
        ):
            final_path = build_media_path(
                media_root=str(
                    self.media_root
                ),
                classification=classification,
                original_filename=filename,
            )

            if (
                classification.media_type
                == MediaType.TV
            ):
                episode = (
                    classification.episode
                )

                if episode is not None:
                    if (
                        episode.episode_end
                        is not None
                    ):
                        episode_text = (
                            f"S"
                            f"{(episode.season or 0):02d}"
                            f"E"
                            f"{episode.episode_start:02d}"
                            f"-E"
                            f"{episode.episode_end:02d}"
                        )
                    else:
                        episode_text = (
                            f"S"
                            f"{(episode.season or 0):02d}"
                            f"E"
                            f"{episode.episode_start:02d}"
                        )

                    print(
                        f"[job {job.id}] "
                        f"classified as TV: "
                        f"{classification.title} "
                        f"{episode_text}",
                        flush=True,
                    )

            elif (
                classification.media_type
                == MediaType.MOVIE
            ):
                print(
                    f"[job {job.id}] "
                    f"classified as MOVIE: "
                    f"{classification.title} "
                    f"({classification.year})",
                    flush=True,
                )

            return final_path

        print(
            f"[job {job.id}] "
            "classification unknown; "
            "keeping in incoming",
            flush=True,
        )

        return (
            self.incoming_dir
            / filename
        )

    async def _download_resumable(
        self,
        message,
        job: JobRecord,
        part_path: Path,
    ) -> None:
        total_size = (
            message.file.size
            if message.file
            else job.file_size
        )

        if not total_size:
            raise RuntimeError(
                "Unable to determine file size"
            )

        existing_size = (
            part_path.stat().st_size
            if part_path.exists()
            else 0
        )

        if existing_size > total_size:
            print(
                f"[job {job.id}] "
                "partial file larger "
                "than source; restarting",
                flush=True,
            )

            part_path.unlink()
            existing_size = 0

        if existing_size == total_size:
            print(
                f"[job {job.id}] "
                "partial file already complete",
                flush=True,
            )
            return

        if existing_size:
            print(
                f"[job {job.id}] "
                f"resuming from "
                f"{existing_size / 1024 / 1024:.1f} MB",
                flush=True,
            )

        reporter = ProgressReporter(
            job=job,
            initial_bytes=existing_size,
        )

        mode = (
            "ab"
            if existing_size
            else "wb"
        )

        with part_path.open(
            mode
        ) as output:
            async for chunk in (
                self.client.iter_download(
                    message.media,
                    offset=existing_size,
                    request_size=REQUEST_SIZE,
                    chunk_size=REQUEST_SIZE,
                    file_size=total_size,
                )
            ):
                output.write(chunk)

                current_size = (
                    output.tell()
                )

                reporter.report(
                    current=current_size,
                    total=total_size,
                )

                if (
                    current_size
                    >= total_size
                ):
                    break

            output.flush()
            os.fsync(
                output.fileno()
            )

        final_size = (
            part_path.stat().st_size
        )

        if final_size != total_size:
            raise RuntimeError(
                "Incomplete download: "
                f"{final_size} of "
                f"{total_size} bytes"
            )

    async def process(
        self,
        job: JobRecord,
    ) -> None:
        started = time.monotonic()

        filename = safe_filename(
            job.original_filename,
            job.id,
        )

        final_path = self.get_final_path(
            job,
            filename,
        )

        final_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        part_path = (
            self.part_dir
            / f"{job.id}-{filename}.part"
        )

        if final_path.exists():
            await self.database.mark_available(
                job.id,
                str(final_path),
            )
            return

        if not self.has_enough_disk_space():
            free_gb = (
                self.get_free_disk_gb()
            )

            error = (
                "Insufficient free disk "
                f"space: {free_gb:.1f} GB"
            )

            await self.database.mark_failed(
                job.id,
                error,
            )

            return

        for attempt in range(
            1,
            self.config.max_retries + 1,
        ):
            await self.database.increment_attempt(
                job.id
            )

            if attempt > 1:
                await self.database.mark_downloading(
                    job.id
                )

            try:
                print(
                    f"[job {job.id}] "
                    f"download attempt "
                    f"{attempt}/"
                    f"{self.config.max_retries}: "
                    f"{filename}",
                    flush=True,
                )

                message = (
                    await self.client.get_messages(
                        job.telegram_chat_id,
                        ids=job.telegram_message_id,
                    )
                )

                if not message:
                    raise RuntimeError(
                        "Telegram message not found"
                    )

                if not message.media:
                    raise RuntimeError(
                        "Telegram message has no media"
                    )

                await self._download_resumable(
                    message=message,
                    job=job,
                    part_path=part_path,
                )

                await self.database.mark_processing(
                    job.id
                )

                os.replace(
                    part_path,
                    final_path,
                )

                await self.database.mark_available(
                    job.id,
                    str(final_path),
                )

                duration = (
                    time.monotonic()
                    - started
                )

                print(
                    f"[job {job.id}] "
                    f"available: "
                    f"{final_path}",
                    flush=True,
                )

                # Ask Jellyfin to scan the libraries.
                await self.jellyfin.refresh_library()

                await self.client.send_message(
                    job.telegram_chat_id,
                    available_message(
                        final_path.name,
                        duration,
                    ),
                    reply_to=(
                        job.telegram_message_id
                    ),
                )

                return

            except asyncio.CancelledError:
                print(
                    f"[job {job.id}] "
                    "download interrupted; "
                    "partial file preserved",
                    flush=True,
                )
                raise

            except Exception as exc:
                error = (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

                print(
                    f"[job {job.id}] "
                    f"attempt {attempt} failed: "
                    f"{error}",
                    flush=True,
                )

                if (
                    attempt
                    >= self.config.max_retries
                ):
                    await self.database.mark_failed(
                        job.id,
                        error,
                    )

                    await self.client.send_message(
                        job.telegram_chat_id,
                        failed_message(
                            filename,
                            attempt,
                        ),
                        reply_to=(
                            job.telegram_message_id
                        ),
                    )

                    return

                await self.database.mark_retry(
                    job.id,
                    error,
                )

                delay = (
                    self.config.retry_base_seconds
                    * attempt
                )

                await asyncio.sleep(
                    delay
                )
