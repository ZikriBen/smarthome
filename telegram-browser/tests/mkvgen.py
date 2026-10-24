"""Tiny Matroska writer for tests (synthetic files, no ffmpeg needed)."""

import zlib


def vint_size(n: int, unknown: bool = False) -> bytes:
    if unknown:
        return b"\x01\xff\xff\xff\xff\xff\xff\xff"
    for length in range(1, 9):
        if n < (1 << (7 * length)) - 1:
            return ((1 << (7 * length)) | n).to_bytes(length, "big") if length > 1 else bytes([0x80 | n])
    raise ValueError(n)


def eid(i: int) -> bytes:
    return i.to_bytes((i.bit_length() + 7) // 8, "big")


def el(i: int, payload: bytes, unknown: bool = False) -> bytes:
    return eid(i) + vint_size(len(payload), unknown) + payload


def u(i: int, v: int) -> bytes:
    return el(i, v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big"))


def s(i: int, v: str) -> bytes:
    return el(i, v.encode())


def block(track: int, rel: int, payload: bytes, flags: int = 0x80) -> bytes:
    return bytes([0x80 | track]) + rel.to_bytes(2, "big", signed=True) + bytes([flags]) + payload


def track(num, ttype, codec, lang=None, bcp=None, forced=None, default=None, comp=None):
    body = u(0xD7, num) + u(0x83, ttype) + s(0x86, codec)
    if lang:
        body += s(0x22B59C, lang)
    if bcp:
        body += s(0x22B59D, bcp)
    if forced is not None:
        body += u(0x55AA, forced)
    if default is not None:
        body += u(0x88, default)
    if comp is not None:
        algo, settings = comp
        cc = u(0x4254, algo) + (el(0x4255, settings) if settings else b"")
        body += el(0x6D80, el(0x6240, el(0x5034, cc)))
    return el(0xAE, body)


def make_mkv(*, with_subs=True, unknown_cluster=False, attachment_bytes=0, video_block=4000, clusters=6):
    """Video track 1; heb (zlib) = 2; eng (header stripping 'He') = 3; PGS = 4."""
    head = el(0x1A45DFA3, s(0x4282, "matroska"))
    info = el(0x1549A966, u(0x2AD7B1, 1_000_000))
    tracks = track(1, 1, "V_MPEG4/ISO/AVC")
    if with_subs:
        tracks += track(2, 0x11, "S_TEXT/UTF8", lang="heb", comp=(0, None))
        tracks += track(3, 0x11, "S_TEXT/UTF8", lang="eng", bcp="en", default=0, comp=(3, b"He"))
        tracks += track(4, 0x11, "S_HDMV/PGS", lang="heb")
        tracks += track(5, 0x11, "S_TEXT/UTF8", lang="heb", forced=1)
    body = info + el(0x1654AE6B, tracks)
    if attachment_bytes:
        body += el(0x1941A469, b"\x00" * attachment_bytes)
    for c in range(clusters):
        ts = c * 2000
        kids = u(0xE7, ts)
        kids += el(0xA3, block(1, 0, b"V" * video_block))
        if with_subs:
            kids += el(0xA0, el(0xA1, block(2, 100, zlib.compress(f"שורה {c}".encode()))) + u(0x9B, 1500))
            kids += el(0xA3, block(3, 200, f"llo {c}".encode()))  # 'He' + 'llo' after header stripping
            kids += el(0xA3, block(4, 300, b"\x00PGS"))
        kids += el(0xA3, block(1, 40, b"v" * video_block))
        body += el(0x1F43B675, kids, unknown=unknown_cluster)
    body += el(0x1C53BB6B, b"\x00" * 64)  # Cues: skipped
    return head + el(0x18538067, body, unknown=True)
