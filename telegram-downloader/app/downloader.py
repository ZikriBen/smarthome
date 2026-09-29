import asyncio
import os
import re
import shutil
import time
from pathlib import Path

from telethon import TelegramClient

from app.config import Config
from app.database import Database
from app.models import JobRecord
from app.notifications import (
    available_message,
    failed_message,
)


REQUEST_SIZE = 512 * 1024  # 512 KiB


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
            now - self.last_print < self.interval
            and current != total
        ):
            return

        self.last_print = now

        elapsed = max(
            now - self.started,
            0.001,
        )

        transferred_this_run = max(
            current - self.initial_bytes,
            0,
        )

        speed_mb_s = (
            transferred_this_run
            / 1024
            / 1024
            / elapsed
        )

        current_mb = current / 1024 / 1024
        total_mb = total / 1024 / 1024

        percent = (
            current / total * 100
            if total
            else 0
        )

        print(
            f"[job {self.job.id}] "
            f"{current_mb:.1f}/{total_mb:.1f} MB "
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

        self.incoming_dir = (
            Path(config.media_root)
            / "incoming"
        )

        self.part_dir = (
            self.incoming_dir
            / ".part"
        )

        self.incoming_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.part_dir.mkdir(
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
                "partial file larger than source; "
                "starting over",
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

        with part_path.open(mode) as output:
            async for chunk in self.client.iter_download(
                message.media,
                offset=existing_size,
                request_size=REQUEST_SIZE,
                chunk_size=REQUEST_SIZE,
                file_size=total_size,
            ):
                output.write(chunk)

                current_size = output.tell()

                reporter.report(
                    current=current_size,
                    total=total_size,
                )

                if current_size >= total_size:
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

    async def _notify_low_disk(
        self,
        job: JobRecord,
        filename: str,
    ) -> None:
        free_gb = self.get_free_disk_gb()

        await self.client.send_message(
            job.telegram_chat_id,
            (
                "⚠️ Download rejected\n\n"
                f"{filename}\n\n"
                "Server storage is too low.\n"
                f"Free: {free_gb:.1f} GB\n"
                f"Minimum required: "
                f"{self.config.min_free_disk_gb} GB"
            ),
            reply_to=(
                job.telegram_message_id
            ),
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

        final_path = (
            self.incoming_dir
            / filename
        )

        part_path = (
            self.part_dir
            / f"{job.id}-{filename}.part"
        )

        if final_path.exists():
            print(
                f"[job {job.id}] "
                f"final file already exists: "
                f"{final_path}",
                flush=True,
            )

            await self.database.mark_available(
                job.id,
                str(final_path),
            )

            return

        if not self.has_enough_disk_space():
            free_gb = self.get_free_disk_gb()

            error = (
                "Insufficient free disk space: "
                f"{free_gb:.1f} GB free, "
                f"minimum "
                f"{self.config.min_free_disk_gb} GB"
            )

            print(
                f"[job {job.id}] {error}",
                flush=True,
            )

            await self.database.mark_failed(
                job.id,
                error,
            )

            await self._notify_low_disk(
                job,
                filename,
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
                        ids=(
                            job.telegram_message_id
                        ),
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

                avg_speed_mb_s = 0.0

                if (
                    job.file_size
                    and duration > 0
                ):
                    avg_speed_mb_s = (
                        job.file_size
                        / 1024
                        / 1024
                        / duration
                    )

                print(
                    f"[job {job.id}] "
                    f"available: {final_path} "
                    f"| {duration:.0f}s "
                    f"| avg "
                    f"{avg_speed_mb_s:.1f} MB/s",
                    flush=True,
                )

                await self.client.send_message(
                    job.telegram_chat_id,
                    available_message(
                        filename,
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

                print(
                    f"[job {job.id}] "
                    f"retrying in {delay}s",
                    flush=True,
                )

                await asyncio.sleep(delay)
