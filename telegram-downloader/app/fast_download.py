import asyncio
import math
import os
from pathlib import Path
from typing import Callable

from telethon import TelegramClient, utils
from telethon.crypto import AuthKey
from telethon.errors import FloodWaitError
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (
    ExportAuthorizationRequest,
    ImportAuthorizationRequest,
)
from telethon.tl.functions.upload import GetFileRequest


CHUNK_SIZE = 512 * 1024


ProgressCallback = Callable[[int, int], None]


class SenderPool:
    def __init__(
        self,
        client: TelegramClient,
        dc_id: int,
    ):
        self.client = client
        self.dc_id = dc_id

        self.auth_key: AuthKey | None = (
            client.session.auth_key
            if client.session.dc_id == dc_id
            else None
        )

        self.senders: list[
            MTProtoSender
        ] = []

    async def create_sender(
        self,
    ) -> MTProtoSender:
        dc = await self.client._get_dc(
            self.dc_id
        )

        sender = MTProtoSender(
            self.auth_key,
            loggers=self.client._log,
        )

        connection = self.client._connection(
            dc.ip_address,
            dc.port,
            dc.id,
            loggers=self.client._log,
            proxy=self.client._proxy,
        )

        await sender.connect(
            connection
        )

        if self.auth_key is None:
            auth = await self.client(
                ExportAuthorizationRequest(
                    self.dc_id
                )
            )

            init_request = (
                self.client._init_request
            )

            init_request.query = (
                ImportAuthorizationRequest(
                    id=auth.id,
                    bytes=auth.bytes,
                )
            )

            request = InvokeWithLayerRequest(
                LAYER,
                init_request,
            )

            await sender.send(
                request
            )

            self.auth_key = (
                sender.auth_key
            )

        self.senders.append(
            sender
        )

        return sender

    async def close(self) -> None:
        await asyncio.gather(
            *[
                sender.disconnect()
                for sender
                in self.senders
            ],
            return_exceptions=True,
        )


class FastTelegramDownloader:
    def __init__(
        self,
        client: TelegramClient,
        connections: int = 2,
    ):
        if connections < 1:
            raise ValueError(
                "connections must be >= 1"
            )

        self.client = client
        self.connections = connections

    @staticmethod
    def _total_chunks(
        file_size: int,
    ) -> int:
        return math.ceil(
            file_size / CHUNK_SIZE
        )

    @staticmethod
    def _chunks_for_lane(
        total_chunks: int,
        lane: int,
        lane_count: int,
    ) -> int:
        if lane >= total_chunks:
            return 0

        return (
            (total_chunks - 1 - lane)
            // lane_count
        ) + 1

    @staticmethod
    def _expected_lane_size(
        *,
        file_size: int,
        lane: int,
        lane_count: int,
    ) -> int:
        total_chunks = (
            FastTelegramDownloader
            ._total_chunks(file_size)
        )

        chunk_count = (
            FastTelegramDownloader
            ._chunks_for_lane(
                total_chunks,
                lane,
                lane_count,
            )
        )

        if chunk_count == 0:
            return 0

        size = (
            chunk_count
            * CHUNK_SIZE
        )

        last_global_chunk = (
            lane
            + (chunk_count - 1)
            * lane_count
        )

        if (
            last_global_chunk
            == total_chunks - 1
        ):
            final_chunk_size = (
                file_size
                - (
                    (total_chunks - 1)
                    * CHUNK_SIZE
                )
            )

            size -= (
                CHUNK_SIZE
                - final_chunk_size
            )

        return size

    @staticmethod
    def _normalize_lane_file(
        *,
        path: Path,
        expected_size: int,
    ) -> int:
        if not path.exists():
            return 0

        size = path.stat().st_size

        if size > expected_size:
            path.unlink()
            return 0

        if size == expected_size:
            return size

        # A crash may have left an incomplete chunk.
        safe_size = (
            size
            // CHUNK_SIZE
            * CHUNK_SIZE
        )

        if safe_size != size:
            with path.open("r+b") as file:
                file.truncate(
                    safe_size
                )

        return safe_size

    async def _download_lane(
        self,
        *,
        sender: MTProtoSender,
        location,
        lane: int,
        lane_count: int,
        file_size: int,
        lane_path: Path,
        progress: ProgressCallback,
    ) -> int:
        expected_size = (
            self._expected_lane_size(
                file_size=file_size,
                lane=lane,
                lane_count=lane_count,
            )
        )

        existing_size = (
            self._normalize_lane_file(
                path=lane_path,
                expected_size=expected_size,
            )
        )

        if (
            existing_size
            == expected_size
        ):
            return existing_size

        completed_chunks = (
            existing_size
            // CHUNK_SIZE
        )

        global_chunk = (
            lane
            + completed_chunks
            * lane_count
        )

        downloaded = existing_size

        lane_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with lane_path.open(
            "ab"
        ) as output:
            while (
                global_chunk
                * CHUNK_SIZE
                < file_size
            ):
                offset = (
                    global_chunk
                    * CHUNK_SIZE
                )

                request = GetFileRequest(
                    location=location,
                    offset=offset,
                    limit=CHUNK_SIZE,
                )

                try:
                    result = (
                        await self.client._call(
                            sender,
                            request,
                        )
                    )

                except FloodWaitError as exc:
                    print(
                        f"Telegram FloodWait: "
                        f"{exc.seconds}s",
                        flush=True,
                    )

                    await asyncio.sleep(
                        exc.seconds
                    )

                    continue

                chunk = result.bytes

                if not chunk:
                    break

                output.write(
                    chunk
                )

                output.flush()

                downloaded += len(
                    chunk
                )

                progress(
                    len(chunk),
                    file_size,
                )

                global_chunk += (
                    lane_count
                )

            os.fsync(
                output.fileno()
            )

        if downloaded != expected_size:
            raise RuntimeError(
                f"Lane {lane} incomplete: "
                f"{downloaded} != "
                f"{expected_size}"
            )

        return downloaded

    def _merge_lanes(
        self,
        *,
        lane_paths: list[Path],
        destination: Path,
        file_size: int,
    ) -> None:
        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        total_chunks = (
            self._total_chunks(
                file_size
            )
        )

        handles = [
            path.open("rb")
            for path in lane_paths
        ]

        try:
            with destination.open(
                "wb"
            ) as output:
                for global_chunk in range(
                    total_chunks
                ):
                    lane = (
                        global_chunk
                        % len(handles)
                    )

                    remaining = (
                        file_size
                        - (
                            global_chunk
                            * CHUNK_SIZE
                        )
                    )

                    size = min(
                        CHUNK_SIZE,
                        remaining,
                    )

                    data = (
                        handles[lane]
                        .read(size)
                    )

                    if len(data) != size:
                        raise RuntimeError(
                            "Lane merge failed: "
                            f"chunk "
                            f"{global_chunk} "
                            f"expected "
                            f"{size} bytes, "
                            f"got "
                            f"{len(data)}"
                        )

                    output.write(
                        data
                    )

                output.flush()
                os.fsync(
                    output.fileno()
                )

        finally:
            for handle in handles:
                handle.close()

        if (
            destination.stat().st_size
            != file_size
        ):
            raise RuntimeError(
                "Merged file size mismatch"
            )

    async def download(
        self,
        *,
        message,
        destination: Path,
        work_prefix: Path,
        progress: ProgressCallback,
    ) -> None:
        if not message.file:
            raise RuntimeError(
                "Telegram file metadata missing"
            )

        file_size = (
            message.file.size
        )

        if not file_size:
            raise RuntimeError(
                "Telegram file size missing"
            )

        dc_id, location = (
            utils.get_input_location(
                message.media
            )
        )

        lane_count = min(
            self.connections,
            self._total_chunks(
                file_size
            ),
        )

        lane_paths = [
            Path(
                f"{work_prefix}.lane-{lane}"
            )
            for lane in range(
                lane_count
            )
        ]

        existing_total = 0

        for lane, path in enumerate(
            lane_paths
        ):
            expected_size = (
                self._expected_lane_size(
                    file_size=file_size,
                    lane=lane,
                    lane_count=lane_count,
                )
            )

            existing_total += (
                self._normalize_lane_file(
                    path=path,
                    expected_size=expected_size,
                )
            )

        if existing_total:
            print(
                "Resuming parallel download "
                f"from "
                f"{existing_total / 1024 / 1024:.1f} MB",
                flush=True,
            )

        pool = SenderPool(
            self.client,
            dc_id,
        )

        try:
            senders = []

            # Create sequentially. This avoids races when
            # authorization must be exported to another DC.
            for _ in range(
                lane_count
            ):
                senders.append(
                    await pool.create_sender()
                )

            await asyncio.gather(
                *[
                    self._download_lane(
                        sender=sender,
                        location=location,
                        lane=lane,
                        lane_count=lane_count,
                        file_size=file_size,
                        lane_path=lane_paths[
                            lane
                        ],
                        progress=progress,
                    )
                    for lane, sender
                    in enumerate(
                        senders
                    )
                ]
            )

            self._merge_lanes(
                lane_paths=lane_paths,
                destination=destination,
                file_size=file_size,
            )

        finally:
            await pool.close()

        for path in lane_paths:
            path.unlink(
                missing_ok=True
            )
