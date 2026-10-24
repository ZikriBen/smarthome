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


_EXT_MIME = {
    "mkv": "video/x-matroska", "mk3d": "video/x-matroska", "webm": "video/webm",
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
    "avi": "video/x-msvideo", "ts": "video/mp2t", "m2ts": "video/mp2t",
}
_MIME_ALIASES = {"video/mkv": "video/x-matroska", "video/matroska": "video/x-matroska", "video/x-mkv": "video/x-matroska"}


def stream_mime(row: dict) -> str:
    """Content-Type to advertise: the file extension wins (Telegram mime types
    for MKV vary: video/x-matroska, video/mkv, ...), else the normalized mime."""
    name = str(row.get("filename") or "")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    mime = str(row.get("mime_type") or "").lower()
    return _MIME_ALIASES.get(mime, mime or "application/octet-stream")


def is_matroska(row: dict) -> bool:
    return stream_mime(row) in ("video/x-matroska", "video/webm")


class _Inflight:
    __slots__ = ("task", "waiters")

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.waiters = 0


def _consume_result(task: asyncio.Task) -> None:
    # A fetch abandoned by all its waiters may still fail; never let that turn
    # into "Task exception was never retrieved" noise.
    if not task.cancelled():
        task.exception()


class StreamService:
    def __init__(
        self,
        *,
        catalog_lookup: Callable[[int], dict | None],
        fetch_message: Callable[[int], Awaitable[Any]],
        source,
        cache: ChunkCache,
        max_streams: int = 2,
        per_stream_requests: int = 16,
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
        self._inflight: dict[tuple[int, int], _Inflight] = {}
        # Foreground (player/Jellyfin HTTP) Telegram fetches in flight. Background
        # readers (subtitle extraction) only fetch while this is zero.
        self._fg_fetching = 0
        self._fg_idle = asyncio.Event()
        self._fg_idle.set()
        self.stats = {"chunks_fetched": 0, "floodwaits": 0, "file_ref_refreshes": 0, "gone": 0, "bg_chunks": 0,
                      "fetches_abandoned": 0, "aborted_responses": 0,
                      "mid_body_retries": 0}

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

    # In-flight fetches are shared between consumers (the player's range
    # requests, Jellyfin's ffmpeg, read-ahead, prefetch, subtitle extraction).
    # Each fetch runs in its OWN task, never inside a consumer's coroutine, and
    # counts its waiters: a consumer that goes away (client disconnect, read-
    # ahead cancelled) only drops its own wait. The fetch is cancelled only when
    # no waiter is left, so a peer's cancellation can never reach the others.
    def _start_fetch(self, key: tuple[int, int], *, foreground: bool) -> "_Inflight":
        entry = _Inflight()
        if foreground:
            self._fg_fetching += 1
            self._fg_idle.clear()

        async def run() -> bytes:
            try:
                data = await self._fetch_chunk(*key)
                if foreground:
                    self.cache.put(key, data)
                else:
                    self.stats["bg_chunks"] += 1
                return data
            finally:
                if self._inflight.get(key) is entry:
                    self._inflight.pop(key, None)
                if foreground:
                    self._fg_fetching -= 1
                    if self._fg_fetching <= 0:
                        self._fg_fetching = 0
                        self._fg_idle.set()

        entry.task = asyncio.get_running_loop().create_task(run())
        entry.task.add_done_callback(_consume_result)
        self._inflight[key] = entry
        return entry

    async def _wait(self, key: tuple[int, int], entry: "_Inflight") -> bytes:
        entry.waiters += 1
        try:
            return await asyncio.shield(entry.task)
        finally:
            entry.waiters -= 1
            if entry.waiters <= 0 and not entry.task.done():
                # Last consumer gone: stop the Telegram read. Unregister first
                # so a newcomer starts a fresh fetch instead of joining a
                # cancelling one.
                if self._inflight.get(key) is entry:
                    self._inflight.pop(key, None)
                self.stats["fetches_abandoned"] += 1
                entry.task.cancel()

    async def get_chunk(self, variant_id: int, index: int) -> bytes:
        key = (variant_id, index)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        entry = self._inflight.get(key)
        if entry is None:
            entry = self._start_fetch(key, foreground=True)
        return await self._wait(key, entry)

    async def get_chunk_background(self, variant_id: int, index: int, *, urgent: bool = False) -> bytes:
        """Low-priority read for bulk consumers (subtitle extraction): waits
        until no foreground fetch is in flight, never takes a stream slot and
        never populates the LRU cache (so it cannot evict the player's chunks).
        Joins (refcounted) an in-flight fetch of the same chunk if there is one.
        ``urgent`` (the Watch-now popup is open, nothing is playing yet) skips
        the foreground-idle wait. Raises StreamError while new streams are
        paused by a FloodWait."""
        key = (variant_id, index)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        entry = self._inflight.get(key)
        if entry is None:
            if not urgent:
                await self._fg_idle.wait()
            self._check_pause()
            hit = self.cache.get(key)
            if hit is not None:
                return hit
            entry = self._inflight.get(key) or self._start_fetch(key, foreground=False)
        return await self._wait(key, entry)

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
                    try:
                        data = await task
                    except (asyncio.CancelledError, StreamError) as exc:
                        if isinstance(exc, asyncio.CancelledError) or exc.status in (410, 503):
                            raise
                        data = await self._retry_chunk(variant_id, idx, exc)
                    except Exception as exc:  # noqa: BLE001 - transient DC/network error
                        data = await self._retry_chunk(variant_id, idx, exc)
                    yield data[s_lo:s_hi]
            finally:
                # Dropping the read-ahead only drops OUR wait on that chunk;
                # other consumers of it are unaffected (see _wait).
                if ahead is not None:
                    if not ahead.done():
                        ahead.cancel()
                    else:
                        _consume_result(ahead)

        return first, rest()

    async def _retry_chunk(self, variant_id: int, idx: int, exc: BaseException) -> bytes:
        """One retry before a mid-body abort: ffmpeg treats an aborted body as
        end of input (exit 0, truncated output), so avoid aborting at all for a
        transient failure. A second failure propagates (-> connection abort)."""
        self.stats["mid_body_retries"] += 1
        self.log(f"[stream] chunk {idx} failed mid-body ({getattr(exc, 'status', type(exc).__name__)}), retrying once")
        await self.sleep(0.5)
        return await self.get_chunk(variant_id, idx)

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
