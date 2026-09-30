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
from app.fast_download import (
    FastTelegramDownloader,
)
from app.jellyfin import JellyfinClient
from app.media_paths import build_media_path
from app.models import JobRecord
from app.reactions import (
    REACTION_AVAILABLE,
    REACTION_FAILED,
    set_status_reaction,
)



def safe_filename(
    filename: str | None,
    job_id: int,
) -> str:
    if not filename:
        return f"telegram-{job_id}.bin"

    filename = Path(
        filename
    ).name

    return re.sub(
        r"[\x00-\x1f/\\\\]",
        "_",
        filename,
    )


class ProgressReporter:
    def __init__(
        self,
        job: JobRecord,
        interval: float = 60.0,
    ):
        self.job = job
        self.interval = interval
        self.started = (
            time.monotonic()
        )
        self.last_print = 0.0
        self.downloaded_this_run = 0

    def add(
        self,
        chunk_size: int,
        total: int,
    ) -> None:
        self.downloaded_this_run += (
            chunk_size
        )

        now = time.monotonic()

        if (
            now - self.last_print
            < self.interval
            and self.downloaded_this_run
            < total
        ):
            return

        self.last_print = now

        elapsed = max(
            now - self.started,
            0.001,
        )

        speed_mb_s = (
            self.downloaded_this_run
            / 1024
            / 1024
            / elapsed
        )

        downloaded_mb = (
            self.downloaded_this_run
            / 1024
            / 1024
        )

        total_mb = (
            total
            / 1024
            / 1024
        )

        percent = (
            self.downloaded_this_run
            / total
            * 100
            if total
            else 0
        )

        print(
            f"[job {self.job.id}] "
            f"{downloaded_mb:.1f}/"
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

        self.jellyfin = (
            JellyfinClient(
                config
            )
        )

        self.fast_downloader = (
            FastTelegramDownloader(
                client=client,
                connections=(
                    config
                    .telegram_download_connections
                ),
            )
        )

        self.media_root = Path(
            config.media_root
        )

        self.incoming_dir = (
            self.media_root
            / "incoming"
        )

        self.movies_dir = (
            self.media_root
            / "movies"
        )

        self.tv_dir = (
            self.media_root
            / "tv"
        )

        self.part_dir = (
            self.incoming_dir
            / ".part"
        )

        for directory in (
            self.incoming_dir,
            self.movies_dir,
            self.tv_dir,
            self.part_dir,
        ):
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )

    def is_interactive_job(
        self,
        job: JobRecord,
    ) -> bool:
        return (
            job.telegram_chat_id
            == self.config.telegram_chat_id
        )

    async def _notify_available(
        self,
        job: JobRecord,
    ) -> None:
        if not self.is_interactive_job(
            job
        ):
            return

        await set_status_reaction(
            self.client,
            job.telegram_chat_id,
            job.telegram_message_id,
            REACTION_AVAILABLE,
        )

    async def _notify_failed(
        self,
        job: JobRecord,
    ) -> None:
        if not self.is_interactive_job(
            job
        ):
            return

        await set_status_reaction(
            self.client,
            job.telegram_chat_id,
            job.telegram_message_id,
            REACTION_FAILED,
        )

    def get_free_disk_gb(
        self,
    ) -> float:
        usage = shutil.disk_usage(
            self.media_root
        )

        return (
            usage.free
            / 1024
            / 1024
            / 1024
        )

    def has_enough_disk_space(
        self,
    ) -> bool:
        return (
            self.get_free_disk_gb()
            >= self.config.min_free_disk_gb
        )

    def get_final_path(
        self,
        job: JobRecord,
        filename: str,
    ) -> Path:
        classification = (
            classify_media(
                filename=(
                    job.original_filename
                ),
                caption=job.caption,
            )
        )

        if (
            classification.media_type
            == MediaType.TV
        ):
            episode = (
                classification.episode
            )

            if (
                classification.title
                and episode
            ):
                final_path = (
                    build_media_path(
                        media_root=str(
                            self.media_root
                        ),
                        classification=(
                            classification
                        ),
                        original_filename=(
                            filename
                        ),
                    )
                )

                season = (
                    episode.season
                    or 0
                )

                if (
                    episode.episode_end
                    is not None
                ):
                    episode_text = (
                        f"S{season:02d}"
                        f"E{episode.episode_start:02d}"
                        f"-E{episode.episode_end:02d}"
                    )

                else:
                    episode_text = (
                        f"S{season:02d}"
                        f"E{episode.episode_start:02d}"
                    )

                print(
                    f"[job {job.id}] "
                    "classified as TV: "
                    f"{classification.title} "
                    f"{episode_text}",
                    flush=True,
                )

                return final_path

        if (
            classification.media_type
            == MediaType.MOVIE
        ):
            if classification.title:
                final_path = (
                    build_media_path(
                        media_root=str(
                            self.media_root
                        ),
                        classification=(
                            classification
                        ),
                        original_filename=(
                            filename
                        ),
                    )
                )

                year_text = (
                    f" ({classification.year})"
                    if classification.year
                    else ""
                )

                print(
                    f"[job {job.id}] "
                    "classified as MOVIE: "
                    f"{classification.title}"
                    f"{year_text}",
                    flush=True,
                )

                return final_path

        print(
            f"[job {job.id}] "
            "classification UNKNOWN; "
            "keeping in incoming",
            flush=True,
        )

        return (
            self.incoming_dir
            / filename
        )

    def get_part_path(
        self,
        job: JobRecord,
        filename: str,
    ) -> Path:
        return (
            self.part_dir
            / f"{job.id}-{filename}.part"
        )

    def get_work_prefix(
        self,
        job: JobRecord,
        filename: str,
    ) -> Path:
        return (
            self.part_dir
            / f"{job.id}-{filename}"
        )

    async def _notify_low_disk(
        self,
        job: JobRecord,
        filename: str,
    ) -> None:
        """
        Low disk is a final failure.

        For the interactive downloader chat we represent
        that as the failed reaction.

        Browser-source jobs remain DB-only.
        """

        await self._notify_failed(
            job
        )

    async def _download_resumable(
        self,
        *,
        message,
        job: JobRecord,
        part_path: Path,
        filename: str,
    ) -> None:
        if not message.file:
            raise RuntimeError(
                "Telegram file metadata missing"
            )

        total_size = (
            message.file.size
        )

        if not total_size:
            raise RuntimeError(
                "Unable to determine file size"
            )

        reporter = (
            ProgressReporter(
                job=job
            )
        )

        work_prefix = (
            self.get_work_prefix(
                job,
                filename,
            )
        )

        def progress(
            chunk_size: int,
            total: int,
        ) -> None:
            reporter.add(
                chunk_size,
                total,
            )

        print(
            f"[job {job.id}] "
            "using "
            f"{self.config.telegram_download_connections} "
            "MTProto sender(s)",
            flush=True,
        )

        await (
            self.fast_downloader
            .download(
                message=message,
                destination=part_path,
                work_prefix=work_prefix,
                progress=progress,
            )
        )

        if (
            not part_path.exists()
            or part_path.stat().st_size
            != total_size
        ):
            raise RuntimeError(
                "Parallel download produced "
                "an invalid output file"
            )

    async def _refresh_jellyfin(
        self,
        job: JobRecord,
    ) -> None:
        """
        Jellyfin refresh is also best-effort.

        A successfully stored media file remains
        AVAILABLE even if Jellyfin is temporarily down.
        """

        try:
            await (
                self.jellyfin
                .refresh_library()
            )

        except Exception as exc:
            print(
                f"[job {job.id}] "
                "Jellyfin refresh error: "
                f"{type(exc).__name__}: "
                f"{exc}",
                flush=True,
            )

    async def _complete_success(
        self,
        job: JobRecord,
        final_path: Path,
    ) -> None:
        """
        This is the important lifecycle boundary:

        1. Media exists at final_path.
        2. Persist AVAILABLE.
        3. Jellyfin refresh is best-effort.
        4. Telegram reaction is best-effort.

        Neither step 3 nor 4 can cause a download retry.
        """

        await (
            self.database
            .mark_available(
                job.id,
                str(final_path),
            )
        )

        await self._refresh_jellyfin(
            job
        )

        await self._notify_available(
            job
        )

    async def _mark_existing_available(
        self,
        job: JobRecord,
        final_path: Path,
    ) -> None:
        print(
            f"[job {job.id}] "
            "final file already exists: "
            f"{final_path}",
            flush=True,
        )

        await self._complete_success(
            job,
            final_path,
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
            self.get_final_path(
                job,
                filename,
            )
        )

        final_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        part_path = (
            self.get_part_path(
                job,
                filename,
            )
        )
        # This check is deliberately before the retry loop.
        #
        # If an earlier attempt successfully moved the file
        # but failed during notification, we immediately
        # recover it as AVAILABLE rather than downloading
        # hundreds of MB again.
        if final_path.exists():
            await (
                self._mark_existing_available(
                    job,
                    final_path,
                )
            )

            return

        if not self.has_enough_disk_space():
            free_gb = (
                self.get_free_disk_gb()
            )

            error = (
                "Insufficient free disk space: "
                f"{free_gb:.1f} GB free, "
                "minimum "
                f"{self.config.min_free_disk_gb} GB"
            )

            print(
                f"[job {job.id}] "
                f"{error}",
                flush=True,
            )

            await (
                self.database
                .mark_failed(
                    job.id,
                    error,
                )
            )

            await self._notify_failed(
                job
            )

            return

        for attempt in range(
            1,
            self.config.max_retries + 1,
        ):
            await (
                self.database
                .increment_attempt(
                    job.id
                )
            )

            if attempt > 1:
                await (
                    self.database
                    .mark_downloading(
                        job.id
                    )
                )

            try:
                print(
                    f"[job {job.id}] "
                    "download attempt "
                    f"{attempt}/"
                    f"{self.config.max_retries}: "
                    f"{filename}",
                    flush=True,
                )

                message = (
                    await self.client
                    .get_messages(
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

                await (
                    self._download_resumable(
                        message=message,
                        job=job,
                        part_path=part_path,
                        filename=filename,
                    )
                )

                await (
                    self.database
                    .mark_processing(
                        job.id
                    )
                )

                os.replace(
                    part_path,
                    final_path,
                )

                duration = (
                    time.monotonic()
                    - started
                )

                size = (
                    final_path
                    .stat()
                    .st_size
                )

                avg_speed = (
                    size
                    / 1024
                    / 1024
                    / duration
                    if duration > 0
                    else 0
                )

                print(
                    f"[job {job.id}] "
                    "available: "
                    f"{final_path} "
                    f"| {duration:.1f}s "
                    "| avg "
                    f"{avg_speed:.2f} MB/s",
                    flush=True,
                )

                # CRITICAL:
                #
                # From this point onward the download is
                # successful. DB availability is committed
                # before any optional integration.
                #
                # _complete_success() internally isolates
                # Jellyfin and Telegram reaction failures.
                await (
                    self._complete_success(
                        job,
                        final_path,
                    )
                )

                return

            except asyncio.CancelledError:
                print(
                    f"[job {job.id}] "
                    "download interrupted; "
                    "parallel lane files preserved",
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

                # IMPORTANT:
                # Do not delete .lane-* files.
                # They are the resume state for the
                # parallel downloader.

                if (
                    attempt
                    >= self.config.max_retries
                ):
                    await (
                        self.database
                        .mark_failed(
                            job.id,
                            error,
                        )
                    )

                    # Best-effort only.
                    # _notify_failed() never raises.
                    await self._notify_failed(
                        job
                    )

                    return

                await (
                    self.database
                    .mark_retry(
                        job.id,
                        error,
                    )
                )

                delay = (
                    self.config
                    .retry_base_seconds
                    * attempt
                )

                print(
                    f"[job {job.id}] "
                    f"retrying in {delay}s",
                    flush=True,
                )

                await asyncio.sleep(
                    delay
                )
