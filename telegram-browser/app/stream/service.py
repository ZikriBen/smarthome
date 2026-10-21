"""Stream service: catalog gate, file-reference refresh, FloodWait policy,
stream slots, chunk cache with read-ahead, and edge prefetch."""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from telethon import utils as tl_utils
from telethon.errors import (
    FileReferenceExpiredError,
    FileReferenceInvalidError,
    FloodPremiumWaitError,
    FloodWaitError,
)

from app.stream.cache import ChunkCache
from app.stream.ranges import CHUNK_SIZE, chunk_plan, chunk_request, edge_chunks, expected_chunk_len

FILE_REF_ERRORS = (FileReferenceExpiredError, FileReferenceInvalidError)
FLOOD_ERRORS = (FloodWaitError, FloodPremiumWaitError)


class StreamError(Exception):
    def __init__(self, status: int, detail: str, retry_after: int | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


@dataclass
class Location:
    dc_id: int
    location: Any
    size: int
    expires: float


def default_extract(message) -> tuple[int, Any, int]:
    dc_id, location = tl_utils.get_input_location(message.media)
    return dc_id, location, int(message.file.size or 0)


def is_media_mime(mime: str | None) -> bool:
    return bool(mime) and mime.lower().startswith("video/")


class StreamService:
    def __init__(
        self,
        *,
        catalog_lookup: Callable[[int], dict | None],
        fetch_message: Callable[[int], Awaitable[Any]],
        source,
        cache: ChunkCache,
        max_streams: int = 2,
        per_stream_requests: int = 4,
        flood_inline_max: int = 10,
        location_ttl: float = 1800.0,
        extract: Callable[[Any], tuple[int, Any, int]] = default_extract,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        log: Callable[[str], None] = lambda m: print(m, flush=True),
    ):
        self.catalog_lookup = catalog_lookup
        self.fetch_message = fetch_message  # bound to SOURCE_CHAT_ID by the caller
        self.source = source
        self.cache = cache
        self.max_streams = max_streams
        self.per_stream_requests = per_stream_requests
        self.flood_inline_max = flood_inline_max
        self.location_ttl = location_ttl
        self.extract = extract
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.paused_until = 0.0
        self.active: dict[int, int] = {}
        self._locations: dict[int, Location] = {}
        self._inflight: dict[tuple[int, int], asyncio.Future] = {}
        self.stats = {"chunks_fetched": 0, "floodwaits": 0, "file_ref_refreshes": 0, "gone": 0}

    # ---------------------------------------------------------------- gates
    def variant(self, variant_id: int) -> dict:
        row = self.catalog_lookup(variant_id)
        if not row or not is_media_mime(row.get("mime_type")):
            raise StreamError(404, "Not found")
        return row

    def _check_pause(self) -> None:
        remaining = self.paused_until - self.clock()
        if remaining > 0:
            raise StreamError(503, "Telegram rate limit, retry later", retry_after=int(remaining) + 1)

    def acquire(self, variant_id: int) -> None:
        self._check_pause()
        count = self.active.get(variant_id, 0)
        if count == 0 and len(self.active) >= self.max_streams:
            raise StreamError(503, "Too many concurrent streams", retry_after=10)
        if count >= self.per_stream_requests:
            raise StreamError(503, "Too many requests for this stream", retry_after=2)
        self.active[variant_id] = count + 1

    def release(self, variant_id: int) -> None:
        count = self.active.get(variant_id, 0) - 1
        if count <= 0:
            self.active.pop(variant_id, None)
        else:
            self.active[variant_id] = count

    # ---------------------------------------------------------------- telegram
    async def location(self, variant_id: int, *, force: bool = False) -> Location:
        cached = self._locations.get(variant_id)
        if cached and not force and cached.expires > self.clock():
            return cached
        try:
            message = await self.fetch_message(variant_id)
        except FLOOD_ERRORS as exc:
            self._flood(exc)
            raise StreamError(503, "Telegram rate limit", retry_after=self._retry_after())
        if message is None:
            self.stats["gone"] += 1
            self._locations.pop(variant_id, None)
            raise StreamError(410, "Gone: the Telegram message was deleted")
        if not getattr(message, "media", None) or not getattr(message, "file", None):
            raise StreamError(410, "Gone: the message no longer has media")
        dc_id, loc, size = self.extract(message)
        if size <= 0:
            raise StreamError(502, "Telegram file size unknown")
        result = Location(dc_id, loc, size, self.clock() + self.location_ttl)
        self._locations[variant_id] = result
        return result

    def _flood(self, exc) -> None:
        seconds = int(getattr(exc, "seconds", 0) or 0)
        self.stats["floodwaits"] += 1
        self.paused_until = max(self.paused_until, self.clock() + seconds)
        self.log(f"[stream] Telegram FloodWait {seconds}s: new streams paused")

    def _retry_after(self) -> int:
        return max(1, int(self.paused_until - self.clock()) + 1)

    async def _fetch_chunk(self, variant_id: int, index: int) -> bytes:
        loc = await self.location(variant_id)
        refreshed = False
        flood_waited = False
        while True:
            offset, limit = chunk_request(index, loc.size)
            try:
                data = await self.source.fetch(loc.dc_id, loc.location, offset, limit)
            except FILE_REF_ERRORS:
                if refreshed:
                    raise StreamError(502, "File reference refresh failed")
                refreshed = True
                self.stats["file_ref_refreshes"] += 1
                loc = await self.location(variant_id, force=True)
                continue
            except FLOOD_ERRORS as exc:
                seconds = int(getattr(exc, "seconds", 0) or 0)
                if seconds <= self.flood_inline_max and not flood_waited:
                    self.stats["floodwaits"] += 1
                    flood_waited = True
                    await self.sleep(seconds)
                    continue
                self._flood(exc)
                raise StreamError(503, "Telegram rate limit", retry_after=self._retry_after())
            want = expected_chunk_len(index, loc.size)
            if len(data) < want:
                raise StreamError(502, "Short read from Telegram")
            self.stats["chunks_fetched"] += 1
            return data[:want]

    async def get_chunk(self, variant_id: int, index: int) -> bytes:
        key = (variant_id, index)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            data = await self._fetch_chunk(variant_id, index)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc if isinstance(exc, Exception) else StreamError(499, "cancelled"))
                future.exception()  # mark retrieved
            raise
        finally:
            self._inflight.pop(key, None)
        self.cache.put(key, data)
        future.set_result(data)
        return data

    # ---------------------------------------------------------------- reading
    async def first_and_rest(self, variant_id: int, start: int, end: int) -> tuple[bytes, AsyncIterator[bytes]]:
        """Fetch the first piece before headers are sent (so errors map to HTTP
        statuses), then return an iterator for the rest with read-ahead of 1."""
        plan = chunk_plan(start, end)
        if not plan:
            async def empty():
                if False:
                    yield b""
            return b"", empty()
        index, lo, hi = plan[0]
        first = (await self.get_chunk(variant_id, index))[lo:hi]

        async def rest():
            ahead: asyncio.Task | None = None
            try:
                for pos in range(1, len(plan)):
                    idx, s_lo, s_hi = plan[pos]
                    task = ahead or asyncio.ensure_future(self.get_chunk(variant_id, idx))
                    ahead = (
                        asyncio.ensure_future(self.get_chunk(variant_id, plan[pos + 1][0]))
                        if pos + 1 < len(plan)
                        else None
                    )
                    data = await task
                    yield data[s_lo:s_hi]
            finally:
                if ahead is not None and not ahead.done():
                    ahead.cancel()

        return first, rest()

    async def prefetch_edges(self, variant_id: int, edge_bytes: int) -> int:
        """Warm the first/last ``edge_bytes`` (Watch-now time only). Returns chunks warmed."""
        try:
            self.acquire(variant_id)
        except StreamError:
            return 0
        try:
            loc = await self.location(variant_id)
            warmed = 0
            for index in edge_chunks(loc.size, edge_bytes, CHUNK_SIZE):
                await self.get_chunk(variant_id, index)
                warmed += 1
            return warmed
        except StreamError as exc:
            self.log(f"[stream] prefetch stopped: {exc.status}")
            return 0
        finally:
            self.release(variant_id)

    def health(self) -> dict:
        return {
            "active_streams": len(self.active),
            "paused_for_s": max(0, int(self.paused_until - self.clock())),
            "cache_mb": round(self.cache.bytes / 1048576, 1),
            "cache_max_mb": round(self.cache.max_bytes / 1048576, 1),
            **self.stats,
        }
