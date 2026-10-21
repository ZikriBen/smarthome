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
ID_SEEKHEAD = 0x114D9B74
ID_SEEK = 0x4DBB
ID_SEEKID = 0x53AB
ID_SEEKPOSITION = 0x53AC
ID_DURATION = 0x4489
ID_CUES = 0x1C53BB6B
ID_CUEPOINT = 0xBB
ID_CUETIME = 0xB3
ID_CUETRACKPOSITIONS = 0xB7
ID_CUETRACK = 0xF7
ID_CUECLUSTERPOSITION = 0xF1
ID_CUERELATIVEPOSITION = 0xF0
ID_CUEDURATION = 0xB2
MAX_CUES_ELEMENT = 32 * 1024 * 1024
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


def hebrew_track(tracks: list[SubTrack]) -> SubTrack | None:
    """The ONE Hebrew text track we turn into a sidecar: a full (non-forced)
    track, default-flagged first, else lowest number; a forced-only Hebrew
    track is used only when there is no full one."""
    heb = [t for t in tracks if t.lang == "heb"]
    if not heb:
        return None
    return sorted(heb, key=lambda t: (t.forced, not t.default, t.number))[0]


@dataclass
class MkvHeader:
    seg_data: int
    seg_end: int
    timescale: int
    duration_ns: float | None
    tracks: list[SubTrack]
    cues_pos: int | None  # absolute file offset of the Cues element, if known
    first_cluster: int | None


async def read_header(reader: "ChunkReader") -> MkvHeader:
    """EBML + Segment head up to the first Cluster: SeekHead, Info, Tracks
    (and Cues if they come first). Usually the first chunk or two."""
    eid, size = await reader.header()
    if eid != ID_EBML:
        raise MkvError("not EBML")
    reader.skip(size)
    eid, size = await reader.header()
    if eid != ID_SEGMENT:
        raise MkvError("no Segment")
    seg_data = reader.pos
    seg_end = reader.size if size == UNKNOWN else min(reader.size, seg_data + size)
    timescale, duration, tracks, cues_pos, first_cluster = 1_000_000, None, None, None, None
    while reader.pos < seg_end:
        start = reader.pos
        eid, size = await reader.header()
        if eid == ID_CLUSTER:
            first_cluster = start
            break
        if size == UNKNOWN:
            raise MkvError("unknown-size top-level element")
        if eid == ID_SEEKHEAD and size <= MAX_HEADER_ELEMENT:
            for cid, seek in iter_children(await reader.read(size)):
                if cid != ID_SEEK:
                    continue
                f = dict(iter_children(seek))
                if uint(f.get(ID_SEEKID, b"")) == ID_CUES and ID_SEEKPOSITION in f:
                    cues_pos = seg_data + uint(f[ID_SEEKPOSITION])
        elif eid == ID_INFO and size <= MAX_HEADER_ELEMENT:
            for cid, val in iter_children(await reader.read(size)):
                if cid == ID_TIMESCALE:
                    timescale = uint(val) or 1_000_000
                elif cid == ID_DURATION and len(val) in (4, 8):
                    import struct
                    duration = struct.unpack(">f" if len(val) == 4 else ">d", val)[0]
        elif eid == ID_TRACKS and size <= MAX_HEADER_ELEMENT:
            tracks = parse_tracks(await reader.read(size))
        elif eid == ID_CUES:
            cues_pos = start
            reader.skip(size)
        else:
            reader.skip(size)
    if tracks is None:
        raise MkvError("no Tracks before the first Cluster")
    return MkvHeader(seg_data, seg_end, timescale, None if duration is None else duration * timescale,
                     tracks, cues_pos, first_cluster)


@dataclass
class CueRef:
    time: int  # in timescale ticks (= the referenced block's absolute timestamp)
    cluster: int  # absolute offset of the Cluster element
    rel: int | None  # CueRelativePosition (from the cluster's data start)
    duration: int | None


async def read_cues(reader: "ChunkReader", hdr: MkvHeader, track: int) -> list[CueRef] | None:
    """Subtitle CuePoints of ``track`` (None if there is no usable Cues)."""
    if hdr.cues_pos is None or hdr.cues_pos >= reader.size:
        return None
    reader.pos = hdr.cues_pos
    eid, size = await reader.header()
    if eid != ID_CUES or size == UNKNOWN or size > MAX_CUES_ELEMENT:
        return None
    out = []
    for cid, point in iter_children(await reader.read(size)):
        if cid != ID_CUEPOINT:
            continue
        t, positions = None, []
        for pid, val in iter_children(point):
            if pid == ID_CUETIME:
                t = uint(val)
            elif pid == ID_CUETRACKPOSITIONS:
                positions.append(dict(iter_children(val)))
        for f in positions:
            if t is None or uint(f.get(ID_CUETRACK, b"")) != track or ID_CUECLUSTERPOSITION not in f:
                continue
            out.append(CueRef(t, hdr.seg_data + uint(f[ID_CUECLUSTERPOSITION]),
                              uint(f[ID_CUERELATIVEPOSITION]) if ID_CUERELATIVEPOSITION in f else None,
                              uint(f[ID_CUEDURATION]) if ID_CUEDURATION in f else None))
    return out


def sparse_usable(cues: list[CueRef] | None, hdr: MkvHeader) -> bool:
    """Trust Cues only when every entry carries CueRelativePosition and the
    density is plausible for a full subtitle track (>= 1 cue per minute);
    otherwise fall back to the full read (a muxer that indexes only some
    subtitle blocks would silently lose lines)."""
    if not cues or any(c.rel is None for c in cues):
        return False
    minutes = (hdr.duration_ns or 0) / 60e9
    return len(cues) >= max(10, minutes * 1.0)


def _block_at(buf: bytes, pos: int, track: int):
    """Parse a SimpleBlock/BlockGroup of ``track`` at ``pos``; returns
    (payload, flags, duration_ticks|None, end) or None if it isn't one."""
    try:
        eid, p = read_vint(buf, pos, True)
        size, p = read_vint(buf, p, False)
    except (MkvError, IndexError):
        return None
    if size == UNKNOWN or size <= 3 or p + size > len(buf) or eid not in (ID_SIMPLEBLOCK, ID_BLOCKGROUP):
        return None
    data, duration = buf[p:p + size], None
    if eid == ID_BLOCKGROUP:
        block = None
        try:
            for cid, val in iter_children(data):
                if cid == ID_BLOCK:
                    block = val
                elif cid == ID_BLOCKDURATION:
                    duration = uint(val)
        except MkvError:
            return None
        if block is None:
            return None
        data = block
    try:
        tnum, _rel, flags, hlen = _block_header(data)
    except (MkvError, IndexError):
        return None
    if tnum != track:
        return None
    return data[hlen:], flags, duration, p + size


class SparseReader:
    """Random access over aligned chunks with a small in-memory map and a
    bounded number of concurrent fetches (prefetch of the planned chunks)."""

    def __init__(self, fetch, size: int, chunk_size: int = CHUNK_SIZE, concurrency=lambda: 1):
        self.fetch, self.size, self.chunk_size, self.concurrency = fetch, size, chunk_size, concurrency
        self.chunks: dict[int, bytes] = {}
        self.fetched_bytes = 0
        self.planned = 0
        self.done = 0

    async def _get(self, idx: int) -> bytes:
        if idx not in self.chunks:
            data = await self.fetch(idx)
            self.chunks[idx] = data
            self.fetched_bytes += len(data)
            self.done += 1
        return self.chunks[idx]

    async def prefetch(self, indices: list[int]) -> None:
        self.planned = len(indices)
        pending = [i for i in indices if i not in self.chunks]
        it = iter(pending)
        running: set[asyncio.Task] = set()
        try:
            while True:
                while len(running) < max(1, int(self.concurrency())):
                    idx = next(it, None)
                    if idx is None:
                        break
                    running.add(asyncio.ensure_future(self._get(idx)))
                if not running:
                    return
                finished, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for t in finished:
                    t.result()  # surface fetch errors
        finally:
            for t in running:
                t.cancel()

    async def window(self, start: int, length: int) -> bytes:
        end = min(self.size, start + length)
        first, last = start // self.chunk_size, (end - 1) // self.chunk_size
        parts = [await self._get(i) for i in range(first, last + 1)]
        blob = b"".join(parts)
        off = start - first * self.chunk_size
        return blob[off:off + (end - start)]


async def extract_sparse(reader: SparseReader, hdr: MkvHeader, track: SubTrack, cues: list[CueRef]) -> bool:
    """Fill ``track.cues`` from the Cues index alone: only the chunks holding
    the track's blocks are read. The cluster size field length is not known
    without reading the cluster head, so each of the 8 possible data starts is
    tried and the one that parses as a block of this track wins. Returns False
    (caller falls back to the full read) if any cue cannot be resolved."""
    plan = sorted({(c.cluster + 4 + 1 + c.rel) // reader.chunk_size for c in cues})
    await reader.prefetch(plan)
    out = []
    for c in sorted(cues, key=lambda c: c.time):
        base = c.cluster + 4 + c.rel
        hit = None
        for vlen in range(1, 9):
            win = await reader.window(base + vlen, 64)
            got = _block_at(win, 0, track.number)
            if got is None:
                continue
            payload, flags, duration, end = got
            if end > len(win):
                continue
            hit = got
            break
        if hit is None:
            # block larger than the 64-byte probe: retry with a bigger window
            for vlen in range(1, 9):
                win = await reader.window(base + vlen, 64 * 1024)
                got = _block_at(win, 0, track.number)
                if got is not None:
                    hit = got
                    break
        if hit is None:
            return False
        payload, flags, duration, _end = hit
        if flags & 0x06:  # laced: not used for text subs
            return False
        dur = duration if duration is not None else c.duration
        out.append((c.time * hdr.timescale, None if dur is None else dur * hdr.timescale, track.decode(payload)))
    track.cues = out
    return True


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


async def extract(reader: ChunkReader, *, should_stop: Callable[[], bool] = lambda: False,
                  want: Callable[[list[SubTrack]], list[SubTrack]] | None = None) -> list[SubTrack]:
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
                parsed = parse_tracks(await reader.read(size))
                picked = want(parsed) if want is not None else pick_tracks(parsed)
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
    """``<stem>.heb.default.srt``: Jellyfin 12.1's ExternalPathParser turns the
    ``.default`` flag into IsDefault, and MediaStreamSelector orders external
    first, then IsDefault, so this one file wins in every SubtitleMode/language
    preference combination (verified against a 12.1.0 server, see STREAMING.md).
    A forced-only Hebrew track becomes ``<stem>.heb.forced.srt`` (no default)."""
    stem = strm_name[:-5] if strm_name.endswith(".strm") else strm_name
    return f"{stem}.{track.lang}.{'forced' if track.forced else 'default'}.srt"


# Same file name as the final sidecar, so Jellyfin lists it from the first scan;
# replaced atomically in place when the extraction finishes (Jellyfin reads the
# file on every subtitle request, no refresh needed). Someone who starts
# playback before that sees this line and can re-pick Hebrew later.
PLACEHOLDER_SRT = ("1\n00:00:00,000 --> 00:00:08,000\n"
                   "כתוביות בעברית עדיין בהכנה.\nבעוד כמה דקות בחרו שוב \"עברית\" בתפריט הכתוביות.\n").encode("utf-8")


# ------------------------------------------------------------------ jobs
def record_sidecars(folder: Path, marker_name: str, variant_id: int, names: list[str], *,
                    pending: bool = False) -> list[str]:
    """Book the sidecars of ``variant_id`` in the marker. ``pending`` records a
    placeholder (not trusted by ``existing``). Sidecars this variant had under
    an older naming (e.g. ``.heb.srt`` / ``.eng.srt`` from b6245d8) are
    deleted, so Jellyfin never sees duplicates. Returns the removed names."""
    marker = folder / marker_name
    from app.stream.watch import MARKER_LOCK

    removed = []
    with MARKER_LOCK:
        try:
            data = json.loads(marker.read_text())
        except (OSError, ValueError):
            data = {"variants": {}}
        key = str(int(variant_id))
        old = set((data.get("subs") or {}).get(key, [])) | set((data.get("subs_pending") or {}).get(key, []))
        for name in sorted(old - set(names)):
            if "/" in name or not name.endswith(".srt"):
                continue
            try:
                (folder / name).unlink()
                removed.append(name)
            except FileNotFoundError:
                pass
        if pending:
            data.setdefault("subs_pending", {})[key] = names
        else:
            data.setdefault("subs", {})[key] = names
            (data.get("subs_pending") or {}).pop(key, None)
        tmp = marker.with_name(f".{marker.name}.subs.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(marker)
    return removed


def write_sidecar(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o644)
    tmp.replace(path)


class SubtitleJobs:
    """At most one extraction at a time (FIFO); per-variant state for the API."""

    def __init__(self, service, *, marker_name: str, max_bytes: int, max_seconds: float,
                 on_done: Callable[[int, Path, list[str]], Awaitable[None]] | None = None,
                 log: Callable[[str], None] = lambda m: print(m, flush=True), clock=time.monotonic,
                 boost_concurrency: int = 6):
        self.service = service
        self.marker_name = marker_name
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.on_done = on_done
        self.log = log
        self.clock = clock
        self.state: dict[int, dict] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.boost_until: dict[int, float] = {}
        self.boost_concurrency = boost_concurrency
        self.header_events: dict[int, asyncio.Event] = {}
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
        # Every outcome is logged: a job that is silently skipped is
        # indistinguishable from one that was never wired (NUC field report).
        if not matroska:
            self.state[vid] = {"status": "unsupported", "detail": "not Matroska"}
            self.log(f"[subs] {vid}: skipped: not Matroska "
                     f"(filename ext / mime {str(row.get('mime_type') or '-')})")
            return self.state[vid]
        done = self.existing(folder, vid)
        if done or (cur and cur["status"] == "none"):
            self.state[vid] = cur if cur and cur["status"] == "none" else {"status": "ready", "files": done}
            self.log(f"[subs] {vid}: already done ({self.state[vid]['status']}, {len(done)} sidecar(s))")
            return self.state[vid]
        size = int(row.get("size") or 0)
        if size and size > self.max_bytes:
            self.state[vid] = {"status": "skipped", "detail": f"file larger than {self.max_bytes >> 20} MB"}
            self.log(f"[subs] {vid}: skipped: {size >> 20} MB > TG_SUBS_MAX_MB={self.max_bytes >> 20}")
            return self.state[vid]
        ahead = [v for v, t in self.tasks.items() if not t.done()]
        self.state[vid] = {"status": "queued", "size_mb": round(size / 1048576) if size else None,
                           "eta_s": int(size / 7e6) if size else None, "behind": len(ahead)}
        self.header_events[vid] = asyncio.Event()
        self.tasks[vid] = asyncio.create_task(self._run(vid, folder, strm_name))
        self.tasks[vid].add_done_callback(lambda t, v=vid: self._task_done(v, t))
        self.log(f"[subs] {vid}: queued ({size >> 20} MB, {len(ahead)} job(s) ahead)")
        return self.state[vid]

    def _task_done(self, vid: int, task: asyncio.Task) -> None:
        if self.tasks.get(vid) is task:
            self.tasks.pop(vid, None)
        if not task.cancelled() and task.exception() is not None:  # never swallow silently
            exc = task.exception()
            self.state[vid] = {"status": "failed", "detail": type(exc).__name__}
            self.log(f"[subs] {vid}: crashed: {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- boost
    def boost(self, variant_id: int, seconds: float = 15.0) -> None:
        """The Watch-now popup is open (it polls): playback has not started,
        so this job may read in parallel and skip the foreground-idle wait.
        Expires on its own once the popup stops polling."""
        self.boost_until[int(variant_id)] = self.clock() + seconds

    def boosted(self, variant_id: int) -> bool:
        return self.boost_until.get(int(variant_id), 0) > self.clock()

    def _concurrency(self, vid: int) -> int:
        return self.boost_concurrency if self.boosted(vid) else 1

    def _fetch(self, vid: int):
        return lambda i: self.service.get_chunk_background(vid, i, urgent=self.boosted(vid))

    def _header_event(self, vid: int) -> asyncio.Event:
        ev = self.header_events.get(vid)
        if ev is None:
            ev = self.header_events[vid] = asyncio.Event()
        return ev

    async def wait_header(self, variant_id: int, timeout: float) -> dict | None:
        """Wait (bounded) until the job knows whether there is a Hebrew track and
        has put the placeholder sidecar in place, so Jellyfin's first scan /
        PlaybackInfo already lists it (its directory cache would otherwise hide
        a sidecar added later; see STREAMING.md)."""
        vid = int(variant_id)
        st = self.state.get(vid)
        if st is None or st["status"] not in ("queued", "running"):
            return self.public(vid)
        try:
            await asyncio.wait_for(self._header_event(vid).wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return self.public(vid)

    # ---------------------------------------------------------------- job
    async def _run(self, vid: int, folder: Path, strm_name: str) -> None:
        header = self._header_event(vid)
        async with self._lock:
            st = self.state[vid]
            t0 = self.clock()
            st.update(status="running", phase="header", started=t0)
            self.log(f"[subs] {vid}: started")
            watchdog = None
            try:
                loc = await self.service.location(vid)
                if loc.size > self.max_bytes:
                    self.state[vid] = {"status": "skipped", "detail": "file too large"}
                    self.log(f"[subs] {vid}: skipped: {loc.size >> 20} MB > TG_SUBS_MAX_MB")
                    return
                fetch = self._fetch(vid)
                reader = ChunkReader(fetch, loc.size)
                st["reader"] = reader
                watchdog = asyncio.create_task(self._progress_log(vid, st, t0))
                hdr = await asyncio.wait_for(read_header(reader), timeout=min(120.0, self.max_seconds))
                heb = hebrew_track(hdr.tracks)
                langs = sorted({t.lang + (".forced" if t.forced else "") for t in hdr.tracks})
                if heb is None:
                    self.state[vid] = {"status": "none", "hebrew": False, "detail": "no Hebrew text subtitle track",
                                       "text_tracks": langs, "read_mb": round(reader.fetched_bytes / 1048576, 1)}
                    self.log(f"[subs] {vid}: no Hebrew text track (text tracks: {langs or 'none'}; "
                             f"read {reader.chunks} chunk(s))")
                    return
                if not folder.is_dir():
                    self.state[vid] = {"status": "failed", "detail": "entry removed"}
                    return
                name = sidecar_name(strm_name, heb)
                if not (folder / name).is_file():
                    await asyncio.to_thread(write_sidecar, folder / name, PLACEHOLDER_SRT)
                removed = await asyncio.to_thread(record_sidecars, folder, self.marker_name, vid, [name], pending=True)
                st.update(hebrew=True, file=name, phase="index")
                header.set()
                self.log(f"[subs] {vid}: Hebrew track #{heb.number} -> {name} (placeholder in place"
                         f"{'; removed ' + ', '.join(removed) if removed else ''})")
                cues = None
                try:
                    cues = await asyncio.wait_for(read_cues(reader, hdr, heb.number), timeout=60)
                except (MkvError, asyncio.TimeoutError) as exc:
                    self.log(f"[subs] {vid}: Cues unreadable ({type(exc).__name__}): full read")
                ok, mode = False, "full"
                if sparse_usable(cues, hdr):
                    mode = "sparse"
                    sparse = SparseReader(fetch, loc.size, concurrency=lambda: self._concurrency(vid))
                    sparse.fetched_bytes = reader.fetched_bytes
                    st.update(reader=sparse, phase="sparse", mode=mode)
                    try:
                        ok = await asyncio.wait_for(extract_sparse(sparse, hdr, heb, cues), timeout=self.max_seconds)
                    except MkvError as exc:
                        self.log(f"[subs] {vid}: sparse read failed ({exc}): full read")
                    fetched = sparse.fetched_bytes
                    if not ok:
                        self.log(f"[subs] {vid}: Cues did not resolve: full read")
                if not ok:
                    mode = "full"
                    full = ChunkReader(fetch, loc.size)
                    st.update(reader=full, phase="full", mode=mode)
                    want = (lambda ts, n=heb.number: [t for t in ts if t.number == n])
                    tracks = await asyncio.wait_for(extract(full, want=want), timeout=self.max_seconds)
                    heb = tracks[0] if tracks else heb
                    fetched = full.fetched_bytes
                secs = self.clock() - t0
                if not heb.cues:
                    self.state[vid] = {"status": "failed", "hebrew": True, "file": name, "detail": "no cues decoded"}
                    self.log(f"[subs] {vid}: Hebrew track had no cues")
                    return
                if not folder.is_dir():
                    self.state[vid] = {"status": "failed", "detail": "entry removed"}
                    return
                await asyncio.to_thread(write_sidecar, folder / name, to_srt(heb.cues))
                await asyncio.to_thread(record_sidecars, folder, self.marker_name, vid, [name])
                self.state[vid] = {"status": "ready", "hebrew": True, "files": [name], "file": name, "mode": mode,
                                   "cues": len(heb.cues), "seconds": round(secs), "read_mb": round(fetched / 1048576)}
                self.log(f"[subs] {vid}: wrote {name} ({len(heb.cues)} cues, {mode}) in {secs:.0f}s, "
                         f"{fetched / 1048576:.0f} MB of {loc.size / 1048576:.0f} MB read")
                if self.on_done:
                    try:
                        await self.on_done(vid, folder, [name])
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"[subs] {vid}: jellyfin refresh failed: {type(exc).__name__}")
            except asyncio.TimeoutError:
                self.state[vid] = {**{k: v for k, v in st.items() if k in ("hebrew", "file")},
                                   "status": "failed", "detail": f"time cap {int(self.max_seconds)}s"}
                self.log(f"[subs] {vid}: time cap reached")
            except asyncio.CancelledError:
                self.state[vid] = {"status": "failed", "detail": "cancelled"}
                raise
            except Exception as exc:  # noqa: BLE001  (StreamError, MkvError, ...)
                detail = getattr(exc, "detail", None) or type(exc).__name__
                self.state[vid] = {**{k: v for k, v in st.items() if k in ("hebrew", "file")},
                                   "status": "failed", "detail": str(detail)}
                self.log(f"[subs] {vid}: failed: {detail}")
            finally:
                header.set()
                st.pop("reader", None)
                if watchdog is not None:
                    watchdog.cancel()

    async def _progress_log(self, vid: int, st: dict, t0: float, every: float = 60.0) -> None:
        while True:
            await asyncio.sleep(every)
            pub = self.public(vid) or {}
            self.log(f"[subs] {vid}: running {st.get('phase')} {100 * (pub.get('progress') or 0):.0f}% after "
                     f"{self.clock() - t0:.0f}s ({(pub.get('bytes') or 0) >> 20} MB read)")

    STATE_NAMES = {"queued": "queued", "running": "running", "ready": "done", "none": "none",
                   "unsupported": "none", "skipped": "failed", "failed": "failed"}

    def public(self, variant_id: int) -> dict | None:
        """API view: ``state`` (queued|running|done|none|failed), ``hebrew``
        (None until the header is parsed), ``bytes``/``total``/``progress``/
        ``eta_s`` while running, ``files`` when done."""
        st = self.state.get(int(variant_id))
        if st is None:
            return None
        out = {k: v for k, v in st.items() if k not in ("reader", "started")}
        out["state"] = self.STATE_NAMES.get(st["status"], st["status"])
        out.setdefault("hebrew", None)
        r = st.get("reader")
        if r is not None:
            if isinstance(r, SparseReader):
                total = max(1, r.planned) * r.chunk_size
                done = r.done * r.chunk_size
            else:
                total, done = r.size, r.pos
            frac = min(1.0, done / total) if total else 0.0
            out.update(bytes=r.fetched_bytes, total=total, progress=round(frac, 3), boosted=self.boosted(variant_id))
            started = st.get("started")
            if started is not None and frac > 0.02:
                el = self.clock() - started
                out["eta_s"] = int(el / frac * (1 - frac))
        return out

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for st in self.state.values():
            counts[st["status"]] = counts.get(st["status"], 0) + 1
        return counts
