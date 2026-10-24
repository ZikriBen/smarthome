"""HTTP Range parsing and Telegram chunk planning.

Range parsing semantics (single range, suffix ranges, 416 with
``bytes */size``) follow RFC 9110 §14 and were cross-checked against
FileToLink's ``parse_range_header`` (Thunder/server/stream_routes.py,
Apache-2.0, https://github.com/Tj-Bots/FileToLink). No code was copied;
see telegram-browser/NOTICE.
"""

import re

# 512 KiB: divisible by 4 KiB, divides 1 MiB, and every request at a multiple
# of 512 KiB stays inside one 1 MiB block (core.telegram.org/api/files).
CHUNK_SIZE = 512 * 1024

_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)


class RangeNotSatisfiable(Exception):
    def __init__(self, size: int):
        super().__init__(f"range not satisfiable for size {size}")
        self.size = size


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return inclusive (start, end), or None to serve the full body (200).

    Malformed and multi-range headers are ignored (RFC 9110 allows a server
    to ignore Range), which yields a plain 200.
    """
    if size <= 0:
        if header and _RANGE_RE.match(header):
            raise RangeNotSatisfiable(size)
        return None
    if not header or "," in header:
        return None
    match = _RANGE_RE.match(header)
    if not match:
        return None
    first, last = match.group(1), match.group(2)
    if first == "" and last == "":
        return None
    if first == "":
        suffix = int(last)
        if suffix == 0:
            raise RangeNotSatisfiable(size)
        return max(size - suffix, 0), size - 1
    start = int(first)
    if start >= size:
        raise RangeNotSatisfiable(size)
    if last == "":
        return start, size - 1
    end = int(last)
    if end < start:
        return None  # syntactically invalid -> ignore
    return start, min(end, size - 1)


def chunk_plan(start: int, end: int, chunk_size: int = CHUNK_SIZE) -> list[tuple[int, int, int]]:
    """Map an inclusive byte range to aligned chunks.

    Returns [(chunk_index, slice_start, slice_end_exclusive)], where the slice
    is relative to the chunk's bytes.
    """
    if end < start:
        return []
    plan = []
    first, last = start // chunk_size, end // chunk_size
    for index in range(first, last + 1):
        base = index * chunk_size
        lo = max(start, base) - base
        hi = min(end, base + chunk_size - 1) - base + 1
        plan.append((index, lo, hi))
    return plan


def chunk_request(index: int, size: int, chunk_size: int = CHUNK_SIZE) -> tuple[int, int]:
    """(offset, limit) for upload.getFile; limit is always the full aligned chunk."""
    offset = index * chunk_size
    if offset >= size:
        raise ValueError("chunk beyond end of file")
    return offset, chunk_size


def expected_chunk_len(index: int, size: int, chunk_size: int = CHUNK_SIZE) -> int:
    return min(chunk_size, size - index * chunk_size)


def edge_chunks(size: int, edge_bytes: int, chunk_size: int = CHUNK_SIZE) -> list[int]:
    """Chunk indices covering the first and last ``edge_bytes`` (deduplicated, ordered)."""
    if size <= 0 or edge_bytes <= 0:
        return []
    total = (size + chunk_size - 1) // chunk_size
    n = min(total, (edge_bytes + chunk_size - 1) // chunk_size)
    head = list(range(n))
    tail_start = max(0, (max(size - edge_bytes, 0)) // chunk_size)
    tail = list(range(tail_start, total))
    return sorted(set(head) | set(tail))
