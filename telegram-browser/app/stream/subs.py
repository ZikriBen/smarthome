"""Watch-time subtitle sidecars: extract text subtitle tracks (S_TEXT/UTF8 =
SubRip) from a Matroska file straight off the Telegram stream and write
``<strm stem>.<lang>[.forced].srt`` next to the .strm, so Jellyfin has them as
external subtitles (selected before embedded ones) instead of ffmpeg-reading
the whole remote file on first play.

Pure Python (no ffmpeg in the image). MKV interleaves subtitle blocks with
audio/video in every cluster, so the whole file has to be read once
(~size / 6-8 MB/s); everything that is not a cluster (attachments, cues, tags)
is skipped without being downloaded, and files without text subtitle tracks
stop right after the track header (first chunk or two). Reads go through
``StreamService.get_chunk_background``: one chunk at a time, only while no
foreground (player/Jellyfin) fetch is in flight, never into the LRU cache.
"""

import asyncio
import json
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.stream.ranges import CHUNK_SIZE

# ------------------------------------------------------------------ EBML ids
ID_EBML = 0x1A45DFA3
ID_SEGMENT = 0x18538067
ID_INFO = 0x1549A966
ID_TIMESCALE = 0x2AD7B1
ID_TRACKS = 0x1654AE6B
ID_TRACKENTRY = 0xAE
ID_TRACKNUMBER = 0xD7
ID_TRACKTYPE = 0x83
ID_CODECID = 0x86
ID_LANGUAGE = 0x22B59C
ID_LANGUAGE_BCP47 = 0x22B59D
ID_NAME = 0x536E
ID_FLAGDEFAULT = 0x88
ID_FLAGFORCED = 0x55AA
ID_CONTENTENCODINGS = 0x6D80
ID_CONTENTENCODING = 0x6240
ID_CONTENTCOMPRESSION = 0x5034
ID_CONTENTCOMPALGO = 0x4254
ID_CONTENTCOMPSETTINGS = 0x4255
ID_CONTENTENCRYPTION = 0x5035
ID_CLUSTER = 0x1F43B675
ID_CLUSTER_TIMESTAMP = 0xE7
ID_SIMPLEBLOCK = 0xA3
ID_BLOCKGROUP = 0xA0
ID_BLOCK = 0xA1
ID_BLOCKDURATION = 0x9B
TOP_LEVEL = {0x114D9B74, ID_INFO, ID_TRACKS, ID_CLUSTER, 0x1C53BB6B, 0x1941A469, 0x1043A770, 0x1254C367}
UNKNOWN = -1
TEXT_CODECS = {"S_TEXT/UTF8", "S_TEXT/ASCII"}
MAX_HEADER_ELEMENT = 8 * 1024 * 1024  # Info/Tracks/BlockGroup read into memory up to this size
MAX_TRACKS = 3

_LANG2 = {"he": "heb", "iw": "heb", "en": "eng", "ar": "ara", "ru": "rus", "fr": "fre", "es": "spa",
          "de": "ger", "it": "ita", "pt": "por", "nl": "dut", "pl": "pol", "tr": "tur", "uk": "ukr"}


class MkvError(Exception):
    pass


def norm_lang(code: str | None) -> str:
    c = (code or "").strip().lower().split("-")[0]
    if c in _LANG2:
        return _LANG2[c]
    if c == "heb":
        return "heb"
    return c if len(c) == 3 and c.isalpha() else "und"


def read_vint(buf: bytes, pos: int, keep_marker: bool) -> tuple[int, int]:
    """Returns (value, new_pos). Size with all value bits set -> UNKNOWN."""
    if pos >= len(buf):
        raise MkvError("truncated vint")
    first = buf[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8:
        raise MkvError("bad vint")
    if pos + length > len(buf):
        raise MkvError("truncated vint")
    value = first if keep_marker else first & (mask - 1)
    for b in buf[pos + 1:pos + length]:
        value = (value << 8) | b
    if not keep_marker and value == (1 << (7 * length)) - 1:
        value = UNKNOWN
    return value, pos + length


def iter_children(buf: bytes):
    pos = 0
    while pos < len(buf):
        eid, pos = read_vint(buf, pos, True)
        size, pos = read_vint(buf, pos, False)
        if size == UNKNOWN or pos + size > len(buf):
            raise MkvError("bad child size")
        yield eid, buf[pos:pos + size]
        pos += size


def uint(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


@dataclass
class SubTrack:
    number: int
    codec: str
    lang: str
    name: str = ""
    default: bool = True
    forced: bool = False
    comp_algo: int | None = None  # None = no compression; 0 zlib; 3 header stripping
    comp_settings: bytes = b""
    cues: list = field(default_factory=list)  # (start_ns, dur_ns|None, text)

    def decode(self, payload: bytes) -> bytes:
        if self.comp_algo == 0:
            return zlib.decompress(payload)
        if self.comp_algo == 3:
            return self.comp_settings + payload
        return payload


def parse_tracks(buf: bytes) -> list[SubTrack]:
    out = []
    for eid, entry in iter_children(buf):
        if eid != ID_TRACKENTRY:
            continue
        f = {"type": 0, "num": 0, "codec": "", "lang": "eng", "bcp": None, "name": "", "default": 1, "forced": 0,
             "algo": None, "settings": b"", "encrypted": False}
        for cid, val in iter_children(entry):
            if cid == ID_TRACKNUMBER:
                f["num"] = uint(val)
            elif cid == ID_TRACKTYPE:
                f["type"] = uint(val)
            elif cid == ID_CODECID:
                f["codec"] = val.decode("ascii", "replace").rstrip("\0")
            elif cid == ID_LANGUAGE:
                f["lang"] = val.decode("ascii", "replace").rstrip("\0")
            elif cid == ID_LANGUAGE_BCP47:
                f["bcp"] = val.decode("ascii", "replace").rstrip("\0")
            elif cid == ID_NAME:
                f["name"] = val.decode("utf-8", "replace").rstrip("\0")
            elif cid == ID_FLAGDEFAULT:
                f["default"] = uint(val)
            elif cid == ID_FLAGFORCED:
                f["forced"] = uint(val)
            elif cid == ID_CONTENTENCODINGS:
                encs = [e for i, e in iter_children(val) if i == ID_CONTENTENCODING]
                if len(encs) != 1:
                    f["encrypted"] = bool(encs)  # several encodings: unsupported
                for enc in encs[:1]:
                    for i, v in iter_children(enc):
                        if i == ID_CONTENTENCRYPTION:
                            f["encrypted"] = True
                        elif i == ID_CONTENTCOMPRESSION:
                            f["algo"] = 0
                            for j, w in iter_children(v):
                                if j == ID_CONTENTCOMPALGO:
                                    f["algo"] = uint(w)
                                elif j == ID_CONTENTCOMPSETTINGS:
                                    f["settings"] = w
        if f["type"] != 0x11 or f["codec"] not in TEXT_CODECS or f["encrypted"] or f["algo"] not in (None, 0, 3):
            continue
        out.append(SubTrack(f["num"], f["codec"], norm_lang(f["bcp"] or f["lang"]), f["name"],
                            bool(f["default"]), bool(f["forced"]), f["algo"], f["settings"]))
    return out


def pick_tracks(tracks: list[SubTrack], limit: int = MAX_TRACKS) -> list[SubTrack]:
    """One track per (language, forced); Hebrew first; at most ``limit``."""
    seen, out = set(), []
    for t in sorted(tracks, key=lambda t: (t.lang != "heb", t.forced, not t.default, t.number)):
        key = (t.lang, t.forced)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out[:limit]


# ------------------------------------------------------------------ reader
class ChunkReader:
    """Sequential reader over aligned chunks; ``skip`` jumps without fetching."""

    def __init__(self, fetch: Callable[[int], Awaitable[bytes]], size: int, chunk_size: int = CHUNK_SIZE):
        self.fetch = fetch
        self.size = size
        self.chunk_size = chunk_size
        self.pos = 0
        self._idx = -1
        self._buf = b""
        self.fetched_bytes = 0
        self.chunks = 0

    async def _load(self, idx: int) -> None:
        if idx != self._idx:
            self._buf = await self.fetch(idx)
            self._idx = idx
            self.fetched_bytes += len(self._buf)
            self.chunks += 1

    async def read(self, n: int) -> bytes:
        if n < 0 or self.pos + n > self.size:
            raise MkvError("read past end")
        out = bytearray()
        while len(out) < n:
            idx, off = divmod(self.pos, self.chunk_size)
            await self._load(idx)
            piece = self._buf[off:off + (n - len(out))]
            if not piece:
                raise MkvError("short chunk")
            out += piece
            self.pos += len(piece)
        return bytes(out)

    def skip(self, n: int) -> None:
        self.pos = min(self.size, self.pos + n)

    async def vint(self, keep_marker: bool) -> int:
        first = await self.read(1)
        length = 1
        mask = 0x80
        while length <= 8 and not first[0] & mask:
            mask >>= 1
            length += 1
        if length > 8:
            raise MkvError("bad vint")
        rest = await self.read(length - 1) if length > 1 else b""
        value, _ = read_vint(first + rest, 0, keep_marker)
        return value

    async def header(self) -> tuple[int, int]:
        return await self.vint(True), await self.vint(False)


def _block_header(data: bytes) -> tuple[int, int, int, int]:
    """(track, rel_timecode, flags, header_len) of a (Simple)Block."""
    track, pos = read_vint(data, 0, False)
    if pos + 3 > len(data):
        raise MkvError("short block")
    rel = int.from_bytes(data[pos:pos + 2], "big", signed=True)
    return track, rel, data[pos + 2], pos + 3


async def extract(reader: ChunkReader, *, should_stop: Callable[[], bool] = lambda: False) -> list[SubTrack]:
    """Parse the file; returns the picked text tracks with their cues filled
    (empty list if there are none: then only the header was read)."""
    eid, size = await reader.header()
    if eid != ID_EBML:
        raise MkvError("not EBML")
    reader.skip(size)
    eid, size = await reader.header()
    if eid != ID_SEGMENT:
        raise MkvError("no Segment")
    seg_end = reader.size if size == UNKNOWN else min(reader.size, reader.pos + size)
    timescale = 1_000_000
    tracks: dict[int, SubTrack] | None = None
    cluster_ts = 0
    cluster_end: int | None = None  # None = not in a cluster; UNKNOWN-size clusters end at a top-level id
    in_unknown_cluster = False
    while reader.pos < seg_end:
        if should_stop():
            raise asyncio.CancelledError()
        if cluster_end is not None and not in_unknown_cluster and reader.pos >= cluster_end:
            cluster_end = None
        eid, size = await reader.header()
        if in_unknown_cluster and eid in TOP_LEVEL:
            in_unknown_cluster = False
            cluster_end = None
        inside = cluster_end is not None or in_unknown_cluster
        if not inside:
            if eid == ID_CLUSTER:
                if tracks is None:
                    raise MkvError("cluster before tracks")
                cluster_ts = 0
                if size == UNKNOWN:
                    in_unknown_cluster, cluster_end = True, None
                else:
                    cluster_end = reader.pos + size
                continue
            if size == UNKNOWN:
                raise MkvError("unknown-size top-level element")
            if eid == ID_INFO and size <= MAX_HEADER_ELEMENT:
                for cid, val in iter_children(await reader.read(size)):
                    if cid == ID_TIMESCALE:
                        timescale = uint(val) or 1_000_000
                continue
            if eid == ID_TRACKS and size <= MAX_HEADER_ELEMENT:
                picked = pick_tracks(parse_tracks(await reader.read(size)))
                if not picked:
                    return []  # nothing to extract: stop after the header
                tracks = {t.number: t for t in picked}
                continue
            reader.skip(size)  # SeekHead, Cues, Attachments, Tags, Chapters, Void...
            continue
        # ---- inside a cluster
        if size == UNKNOWN:
            raise MkvError("unknown-size cluster child")
        if eid == ID_CLUSTER_TIMESTAMP:
            cluster_ts = uint(await reader.read(size))
        elif eid == ID_SIMPLEBLOCK:
            head = await reader.read(min(size, 12))
            track, rel, flags, hlen = _block_header(head)
            sub = tracks.get(track)
            if sub is None or flags & 0x06:
                reader.skip(size - len(head))
            else:
                payload = head[hlen:] + await reader.read(size - len(head))
                sub.cues.append(((cluster_ts + rel) * timescale, None, sub.decode(payload)))
        elif eid == ID_BLOCKGROUP and size <= MAX_HEADER_ELEMENT:
            group = await reader.read(size)
            block, duration = None, None
            for cid, val in iter_children(group):
                if cid == ID_BLOCK:
                    block = val
                elif cid == ID_BLOCKDURATION:
                    duration = uint(val)
            if block is not None:
                track, rel, flags, hlen = _block_header(block)
                sub = tracks.get(track)
                if sub is not None and not flags & 0x06:
                    sub.cues.append(((cluster_ts + rel) * timescale,
                                     None if duration is None else duration * timescale,
                                     sub.decode(block[hlen:])))
        else:
            reader.skip(size)
    return list(tracks.values()) if tracks else []


def _ts(ns: int) -> str:
    ms = max(0, int(round(ns / 1_000_000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list) -> bytes:
    cues = sorted(cues, key=lambda c: c[0])
    out, n = [], 0
    for i, (start, dur, raw) in enumerate(cues):
        text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n").strip("\0 \n")
        if not text:
            continue
        if dur is None or dur <= 0:
            nxt = cues[i + 1][0] if i + 1 < len(cues) else start + 5_000_000_000
            dur = max(500_000_000, min(nxt - start, 5_000_000_000))
        n += 1
        out.append(f"{n}\n{_ts(start)} --> {_ts(start + dur)}\n{text}\n")
    return "\n".join(out).encode("utf-8")


def sidecar_name(strm_name: str, track: SubTrack) -> str:
    stem = strm_name[:-5] if strm_name.endswith(".strm") else strm_name
    return f"{stem}.{track.lang}{'.forced' if track.forced else ''}.srt"


# ------------------------------------------------------------------ jobs
def record_sidecars(folder: Path, marker_name: str, variant_id: int, names: list[str]) -> None:
    marker = folder / marker_name
    from app.stream.watch import MARKER_LOCK

    with MARKER_LOCK:
        try:
            data = json.loads(marker.read_text())
        except (OSError, ValueError):
            data = {"variants": {}}
        data.setdefault("subs", {})[str(int(variant_id))] = names
        tmp = marker.with_name(f".{marker.name}.subs.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(marker)


def write_sidecar(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o644)
    tmp.replace(path)


class SubtitleJobs:
    """At most one extraction at a time (FIFO); per-variant state for the API."""

    def __init__(self, service, *, marker_name: str, max_bytes: int, max_seconds: float,
                 on_done: Callable[[int, Path, list[str]], Awaitable[None]] | None = None,
                 log: Callable[[str], None] = lambda m: print(m, flush=True), clock=time.monotonic):
        self.service = service
        self.marker_name = marker_name
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.on_done = on_done
        self.log = log
        self.clock = clock
        self.state: dict[int, dict] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def status(self, variant_id: int) -> dict | None:
        return self.state.get(int(variant_id))

    def existing(self, folder: Path, variant_id: int) -> list[str]:
        try:
            data = json.loads((folder / self.marker_name).read_text())
        except (OSError, ValueError):
            return []
        names = (data.get("subs") or {}).get(str(int(variant_id)))
        if names is None:
            return []
        return names if all((folder / n).is_file() for n in names) else []

    def schedule(self, variant_id: int, row: dict, folder: Path, strm_name: str, matroska: bool) -> dict:
        vid = int(variant_id)
        cur = self.state.get(vid)
        if cur and cur["status"] in ("queued", "running"):
            return cur
        if not matroska:
            self.state[vid] = {"status": "unsupported", "detail": "not Matroska"}
            return self.state[vid]
        done = self.existing(folder, vid)
        if done or (cur and cur["status"] == "none"):
            self.state[vid] = cur if cur and cur["status"] == "none" else {"status": "ready", "files": done}
            return self.state[vid]
        size = int(row.get("size") or 0)
        if size and size > self.max_bytes:
            self.state[vid] = {"status": "skipped", "detail": f"file larger than {self.max_bytes >> 20} MB"}
            return self.state[vid]
        self.state[vid] = {"status": "queued", "size_mb": round(size / 1048576) if size else None,
                           "eta_s": int(size / 7e6) if size else None}
        self.tasks[vid] = asyncio.create_task(self._run(vid, folder, strm_name))
        self.tasks[vid].add_done_callback(lambda _t, v=vid: self.tasks.pop(v, None))
        return self.state[vid]

    async def _run(self, vid: int, folder: Path, strm_name: str) -> None:
        async with self._lock:
            st = self.state[vid]
            st["status"] = "running"
            t0 = self.clock()
            reader = None
            try:
                loc = await self.service.location(vid)
                if loc.size > self.max_bytes:
                    self.state[vid] = {"status": "skipped", "detail": "file too large"}
                    return
                reader = ChunkReader(lambda i: self.service.get_chunk_background(vid, i), loc.size)
                st["reader"] = reader
                tracks = await asyncio.wait_for(extract(reader), timeout=self.max_seconds)
                secs = self.clock() - t0
                if not tracks:
                    self.state[vid] = {"status": "none", "detail": "no text subtitle tracks",
                                       "read_mb": round(reader.fetched_bytes / 1048576, 1)}
                    self.log(f"[subs] {vid}: no text subtitle tracks (read {reader.chunks} chunks)")
                    return
                if not folder.is_dir():
                    self.state[vid] = {"status": "failed", "detail": "entry removed"}
                    return
                names = []
                for t in tracks:
                    if not t.cues:
                        continue
                    name = sidecar_name(strm_name, t)
                    await asyncio.to_thread(write_sidecar, folder / name, to_srt(t.cues))
                    names.append(name)
                await asyncio.to_thread(record_sidecars, folder, self.marker_name, vid, names)
                self.state[vid] = {"status": "ready", "files": names, "seconds": round(secs),
                                   "read_mb": round(reader.fetched_bytes / 1048576)}
                self.log(f"[subs] {vid}: wrote {len(names)} sidecar(s) in {secs:.0f}s, "
                         f"{reader.fetched_bytes / 1048576:.0f} MB read")
                if self.on_done and names:
                    try:
                        await self.on_done(vid, folder, names)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"[subs] {vid}: jellyfin refresh failed: {type(exc).__name__}")
            except asyncio.TimeoutError:
                self.state[vid] = {"status": "failed", "detail": f"time cap {int(self.max_seconds)}s"}
                self.log(f"[subs] {vid}: time cap reached")
            except asyncio.CancelledError:
                self.state[vid] = {"status": "failed", "detail": "cancelled"}
                raise
            except Exception as exc:  # noqa: BLE001  (StreamError, MkvError, ...)
                detail = getattr(exc, "detail", None) or type(exc).__name__
                self.state[vid] = {"status": "failed", "detail": str(detail)}
                self.log(f"[subs] {vid}: failed: {detail}")
            finally:
                st.pop("reader", None)

    def public(self, variant_id: int) -> dict | None:
        st = self.state.get(int(variant_id))
        if st is None:
            return None
        out = {k: v for k, v in st.items() if k != "reader"}
        r = st.get("reader")
        if r is not None and r.size:
            out["progress"] = round(r.pos / r.size, 3)
        return out

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for st in self.state.values():
            counts[st["status"]] = counts.get(st["status"], 0) + 1
        return counts
