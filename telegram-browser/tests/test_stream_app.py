import asyncio

import httpx
import pytest

from app.stream import signing
from app.stream.ranges import CHUNK_SIZE
from app.stream.server import build_stream_app
from tests.fakes import CONTENT, KEY, SIZE, Clock, FakeSource, fileref, flood, make_service, message


def run(coro):
    return asyncio.run(coro)


def url(vid, key=KEY):
    return f"/s/{vid}/{signing.sign(key, vid)}"


async def req(svc, method, path, headers=None):
    app = build_stream_app(svc, lambda: KEY)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.request(method, path, headers=headers or {})


def test_full_200():
    svc = make_service()
    r = run(req(svc, "GET", url(10)))
    assert r.status_code == 200 and r.content == CONTENT
    assert r.headers["accept-ranges"] == "bytes" and r.headers["content-length"] == str(SIZE)
    assert r.headers["content-type"] == "video/x-matroska" and r.headers["x-content-type-options"] == "nosniff"
    assert "content-range" not in r.headers
    assert svc.active == {}


@pytest.mark.parametrize("rng,lo,hi", [
    ("bytes=0-0", 0, 0), ("bytes=100-200", 100, 200), (f"bytes={CHUNK_SIZE - 3}-{CHUNK_SIZE + 2}", CHUNK_SIZE - 3, CHUNK_SIZE + 2),
    ("bytes=-1000", SIZE - 1000, SIZE - 1), (f"bytes={SIZE - 10}-", SIZE - 10, SIZE - 1), ("bytes=5-999999999", 5, SIZE - 1),
])
def test_ranges_206(rng, lo, hi):
    r = run(req(make_service(), "GET", url(10), {"Range": rng}))
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes {lo}-{hi}/{SIZE}"
    assert r.content == CONTENT[lo:hi + 1] and r.headers["content-length"] == str(hi - lo + 1)


def test_416_and_ignored_ranges():
    svc = make_service()
    r = run(req(svc, "GET", url(10), {"Range": f"bytes={SIZE}-"}))
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{SIZE}"
    r = run(req(svc, "GET", url(10), {"Range": "bytes=0-1,4-5"}))
    assert r.status_code == 200 and len(r.content) == SIZE
    assert svc.active == {}


def test_head_no_body_no_chunk_fetch():
    svc = make_service()
    r = run(req(svc, "HEAD", url(10), {"Range": "bytes=10-19"}))
    assert r.status_code == 206 and r.content == b"" and r.headers["content-length"] == "10"
    assert svc.source.calls == [] and svc.active == {}
    r = run(req(svc, "HEAD", url(10)))
    assert r.status_code == 200 and r.headers["content-length"] == str(SIZE)


def test_bad_sig_unknown_nonmedia_404():
    svc = make_service()
    assert run(req(svc, "GET", f"/s/10/{'0' * 32}")).status_code == 404
    assert run(req(svc, "GET", url(10, b"other-key" * 4))).status_code == 404
    assert run(req(svc, "GET", "/s/10/not-hex")).status_code == 404
    assert run(req(svc, "GET", url(999))).status_code == 404  # not in catalog
    assert run(req(svc, "GET", url(13))).status_code == 404  # image mime
    assert run(req(svc, "GET", "/s/abc/x")).status_code in (404, 422)
    assert svc.fetch_log == []  # never reached Telegram


def test_no_other_routes():
    svc = make_service()
    for path in ("/", "/api/items", "/poster/1", "/docs", "/openapi.json", "/s/10"):
        assert run(req(svc, "GET", path)).status_code == 404


def test_deleted_message_410():
    svc = make_service(messages={10: [None]})
    r = run(req(svc, "GET", url(10)))
    assert r.status_code == 410 and svc.active == {}


def test_file_reference_refreshed_once():
    src = FakeSource()
    src.errors = [fileref()]
    svc = make_service(source=src, messages={10: [message(loc="old"), message(loc="new")]})
    r = run(req(svc, "GET", url(10), {"Range": "bytes=0-9"}))
    assert r.status_code == 206 and r.content == CONTENT[:10]
    assert svc.fetch_log == [10, 10] and svc.stats["file_ref_refreshes"] == 1
    assert [c[1] for c in src.calls] == ["old", "new"]


def test_file_reference_second_failure_is_502_no_loop():
    src = FakeSource()
    src.errors = [fileref(), fileref(), fileref()]
    svc = make_service(source=src)
    r = run(req(svc, "GET", url(10), {"Range": "bytes=0-9"}))
    assert r.status_code == 502 and len(src.calls) == 2 and svc.fetch_log == [10, 10]


def test_floodwait_short_is_waited_inline():
    sleeps = []
    src = FakeSource()
    src.errors = [flood(7)]
    svc = make_service(source=src, sleeps=sleeps)
    r = run(req(svc, "GET", url(10), {"Range": "bytes=0-9"}))
    assert r.status_code == 206 and sleeps == [7] and svc.paused_until == 0


def test_floodwait_long_503_and_pause_until_expiry():
    clock = Clock()
    src = FakeSource()
    src.errors = [flood(60)]
    svc = make_service(source=src, clock=clock)
    r = run(req(svc, "GET", url(10), {"Range": "bytes=0-9"}))
    assert r.status_code == 503 and 55 <= int(r.headers["retry-after"]) <= 61
    r = run(req(svc, "GET", url(11), {"Range": "bytes=0-9"}))  # new stream paused
    assert r.status_code == 503 and "retry-after" in r.headers
    clock.t += 61
    r = run(req(svc, "GET", url(11), {"Range": "bytes=0-9"}))
    assert r.status_code == 206


def test_second_short_floodwait_escalates():
    sleeps = []
    src = FakeSource()
    src.errors = [flood(3), flood(3)]
    svc = make_service(source=src, sleeps=sleeps)
    r = run(req(svc, "GET", url(10), {"Range": "bytes=0-9"}))
    assert r.status_code == 503 and sleeps == [3]


def test_max_two_streams():
    svc = make_service()
    svc.acquire(10)
    svc.acquire(11)
    r = run(req(svc, "GET", url(12), {"Range": "bytes=0-9"}))
    assert r.status_code == 503 and r.headers["retry-after"] == "10"
    r = run(req(svc, "GET", url(10), {"Range": "bytes=0-9"}))  # same stream may add a request
    assert r.status_code == 206
    svc.release(11)
    assert run(req(svc, "GET", url(12), {"Range": "bytes=0-9"})).status_code == 206


def test_mid_stream_error_aborts_connection():
    src = FakeSource()
    src.fail_at_offset = 2 * CHUNK_SIZE  # FloodWait 999 on chunk 2
    svc = make_service(source=src)
    with pytest.raises(Exception):
        run(req(svc, "GET", url(10)))
    assert svc.active == {} and svc.paused_until > 0


def test_cache_and_prefetch_edges():
    svc = make_service()
    warmed = run(svc.prefetch_edges(10, 2 * 1024 * 1024))
    assert warmed == 4  # whole 4-chunk fake file is within the 2 MB edges
    before = len(svc.source.calls)
    r = run(req(svc, "GET", url(10), {"Range": f"bytes=-{1000}"}))
    assert r.status_code == 206 and len(svc.source.calls) == before and svc.cache.hits >= 1
    assert svc.active == {}


def test_prefetch_respects_pause_and_slots():
    clock = Clock()
    svc = make_service(clock=clock)
    svc.paused_until = clock.t + 30
    assert run(svc.prefetch_edges(10, 2 * 1024 * 1024)) == 0 and svc.source.calls == []


def test_location_cached_between_requests():
    svc = make_service()
    run(req(svc, "GET", url(10), {"Range": "bytes=0-9"}))
    run(req(svc, "GET", url(10), {"Range": "bytes=600000-600009"}))
    assert svc.fetch_log == [10]


def test_concurrent_requests_dedupe_inflight():
    svc = make_service()

    async def both():
        a = svc.get_chunk(10, 1)
        b = svc.get_chunk(10, 1)
        return await asyncio.gather(a, b)

    x, y = run(both())
    assert x == y == CONTENT[CHUNK_SIZE:2 * CHUNK_SIZE] and len(svc.source.calls) == 1


# ------------------------------------------------------------------ browser playback headers (field finding 3)
from app.stream.server import etag_for  # noqa: E402
from app.stream.service import stream_mime  # noqa: E402


@pytest.mark.parametrize("row,mime", [
    ({"filename": "Movie.2001.720p.mkv", "mime_type": "video/mp4"}, "video/x-matroska"),
    ({"filename": "x", "mime_type": "video/mkv"}, "video/x-matroska"),
    ({"filename": "a.MP4", "mime_type": "video/x-matroska"}, "video/mp4"),
    ({"filename": None, "mime_type": "video/webm"}, "video/webm"),
    ({"filename": "a.bin", "mime_type": None}, "application/octet-stream"),
])
def test_stream_mime(row, mime):
    assert stream_mime(row) == mime


def test_etag_and_if_range():
    svc = make_service()
    r = run(req(svc, "HEAD", url(10)))
    etag = r.headers["etag"]
    assert etag == etag_for(10, SIZE) and r.headers["content-length"] == str(SIZE)
    r = run(req(svc, "GET", url(10), {"Range": "bytes=0-", "If-Range": etag}))
    assert r.status_code == 206 and r.headers["content-range"] == f"bytes 0-{SIZE - 1}/{SIZE}"
    r = run(req(svc, "GET", url(10), {"Range": "bytes=10-19", "If-Range": '"other"'}))
    assert r.status_code == 200 and len(r.content) == SIZE


def test_cors_only_for_configured_origin():
    svc = make_service()

    async def go(origin):
        app = build_stream_app(svc, lambda: KEY, frozenset({"http://<LAN_IP>:8096"}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.head(url(10), headers={"Origin": origin})

    ok = run(go("http://<LAN_IP>:8096"))
    assert ok.headers["access-control-allow-origin"] == "http://<LAN_IP>:8096"
    assert ok.headers["access-control-allow-credentials"] == "true" and "Content-Range" in ok.headers["access-control-expose-headers"]
    bad = run(go("http://evil.example"))
    assert "access-control-allow-origin" not in bad.headers
    plain = run(req(svc, "HEAD", url(10)))
    assert "access-control-allow-origin" not in plain.headers


def test_many_parallel_requests_one_stream_not_rejected():
    """Browser + Jellyfin probe + subtitle extraction open >4 requests on one
    item (first-play stall). The per-stream cap is now 16, not 4."""
    svc = make_service()

    async def scenario():
        return await asyncio.gather(*[req(svc, "GET", url(10), {"Range": f"bytes={i * 1000}-{i * 1000 + 99}"})
                                      for i in range(8)])

    rs = run(scenario())
    assert [r.status_code for r in rs] == [206] * 8 and svc.active == {}
