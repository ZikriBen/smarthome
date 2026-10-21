import pytest

from app.stream.ranges import (
    CHUNK_SIZE, RangeNotSatisfiable, chunk_plan, chunk_request, edge_chunks, expected_chunk_len, parse_range,
)

S = 10_000_000


@pytest.mark.parametrize("header,expected", [
    (None, None), ("", None), ("bytes=0-", (0, S - 1)), ("bytes=0-0", (0, 0)),
    ("bytes=100-199", (100, 199)), ("bytes=-500", (S - 500, S - 1)), ("bytes=-99999999", (0, S - 1)),
    ("bytes=9999990-20000000", (9999990, S - 1)), ("BYTES = 5 - 9", (5, 9)),
    ("bytes=0-1,5-9", None), ("items=0-1", None), ("bytes=abc", None), ("bytes=-", None), ("bytes=9-5", None),
])
def test_parse(header, expected):
    assert parse_range(header, S) == expected


@pytest.mark.parametrize("header", ["bytes=10000000-", "bytes=20000000-20000001", "bytes=-0"])
def test_unsatisfiable(header):
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, S)


def test_empty_file():
    assert parse_range(None, 0) is None
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=0-", 0)


@pytest.mark.parametrize("start,end", [(0, 0), (0, S - 1), (524287, 524288), (1048575, 1048577), (S - 1, S - 1), (123, 4567890)])
def test_chunk_plan_covers_exactly_and_is_aligned(start, end):
    plan = chunk_plan(start, end)
    total = sum(hi - lo for _, lo, hi in plan)
    assert total == end - start + 1
    assert plan[0][0] == start // CHUNK_SIZE and plan[-1][0] == end // CHUNK_SIZE
    for index, lo, hi in plan:
        offset, limit = chunk_request(index, S)
        assert offset % 4096 == 0 and limit % 4096 == 0
        assert (1024 * 1024) % limit == 0
        assert offset // (1024 * 1024) == (offset + limit - 1) // (1024 * 1024)
        assert 0 <= lo < hi <= CHUNK_SIZE


def test_chunk_request_beyond_end():
    with pytest.raises(ValueError):
        chunk_request(S // CHUNK_SIZE + 1, S)


def test_expected_len_last_chunk():
    last = (S - 1) // CHUNK_SIZE
    assert expected_chunk_len(last, S) == S - last * CHUNK_SIZE
    assert expected_chunk_len(0, S) == CHUNK_SIZE


def test_edge_chunks():
    two_mb = 2 * 1024 * 1024
    total = (S + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks = edge_chunks(S, two_mb)
    assert chunks[:4] == [0, 1, 2, 3]
    assert chunks[-1] == total - 1 and len(chunks) <= 9
    assert edge_chunks(1000, two_mb) == [0]
    assert edge_chunks(0, two_mb) == []
