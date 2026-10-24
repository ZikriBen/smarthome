"""Regression: a peer's cancellation must never truncate another consumer.

Field bug (NUC, ef7acdd, variant 34810): the browser player's read-ahead task
was cancelled on disconnect while it OWNED the shared in-flight fetch of a
chunk; Jellyfin's ffmpeg (subtitle extraction) was waiting on the same chunk,
got StreamError('cancelled'), its response ended, and Jellyfin cached a
truncated .srt (113 of ~1000 cues).
"""
import asyncio
import socket
import threading
import time

import httpx
import pytest

from app.stream import signing
from app.stream.ranges import CHUNK_SIZE
from app.stream.server import build_stream_app
from app.stream.service import StreamError
from tests.fakes import CONTENT, KEY, SIZE, FakeSource, make_service


def run(coro):
    return asyncio.run(coro)


def chunk(i):
    return CONTENT[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]


class GatedSource(FakeSource):
    """fetch() blocks until the test opens the gate."""

    def __init__(self):
        super().__init__()
        self.gate = None

    async def fetch(self, dc_id, location, offset, limit):
        if self.gate is None:
            self.gate = asyncio.Event()
        self.calls.append((dc_id, location, offset, limit))
        await self.gate.wait()
        if self.fail_at_offset is not None and offset >= self.fail_at_offset:
            raise RuntimeError("boom")
        return self.content[offset:offset + limit]


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


# ----------------------------------------------------------- (1) shared fetch
def test_owner_cancel_does_not_poison_the_peer():
    src = GatedSource()
    svc = make_service(source=src)

    async def go():
        owner = asyncio.ensure_future(svc.get_chunk(10, 1))  # starts the fetch
        await settle()
        peer = asyncio.ensure_future(svc.get_chunk(10, 1))  # joins it
        await settle()
        owner.cancel()  # browser disconnects
        await settle()
        src.gate.set()
        data = await peer
        with pytest.raises(asyncio.CancelledError):
            await owner
        return data

    assert run(go()) == chunk(1)
    assert len(src.calls) == 1 and svc.stats["fetches_abandoned"] == 0
    assert svc.cache.get((10, 1)) == chunk(1) and svc._inflight == {} and svc._fg_idle.is_set()


def test_readahead_of_a_closed_response_does_not_truncate_the_other():
    """The exact NUC pattern: response A's rest() owns the read-ahead of chunk
    2; A's client goes away (aclose); response B is waiting for chunk 2."""
    src = GatedSource()
    svc = make_service(source=src)

    async def go():
        first, rest_a = await asyncio.wait_for(_open(svc, src, 0, SIZE - 1), 1)
        piece = await asyncio.wait_for(_next_with_gate(src, rest_a), 1)  # chunk 1, read-ahead of 2 started
        assert piece == chunk(1)
        src.gate.clear()
        b = asyncio.ensure_future(svc.get_chunk(10, 2))  # the "ffmpeg" consumer
        await settle()
        await rest_a.aclose()  # browser drops the connection -> read-ahead cancelled
        await settle()
        src.gate.set()
        return await b

    assert run(go()) == chunk(2)
    assert svc.stats["fetches_abandoned"] == 0


async def _open(svc, src, start, end):
    task = asyncio.ensure_future(svc.first_and_rest(10, start, end))
    await settle()
    src.gate.set()
    return await task


async def _next_with_gate(src, it):
    return await it.__anext__()


def test_last_waiter_gone_cancels_the_fetch_and_a_newcomer_refetches():
    src = GatedSource()
    svc = make_service(source=src)

    async def go():
        a = asyncio.ensure_future(svc.get_chunk(10, 3))
        b = asyncio.ensure_future(svc.get_chunk(10, 3))
        await settle()
        a.cancel()
        b.cancel()
        await settle()
        assert svc.stats["fetches_abandoned"] == 1 and (10, 3) not in svc._inflight
        assert svc._fg_idle.is_set()
        src.gate.set()
        return await svc.get_chunk(10, 3)

    assert run(go()) == CONTENT[3 * CHUNK_SIZE:]
    assert len(src.calls) == 2


def test_background_reader_survives_foreground_cancel():
    src = GatedSource()
    svc = make_service(source=src)

    async def go():
        fg = asyncio.ensure_future(svc.get_chunk(10, 0))
        await settle()
        bg = asyncio.ensure_future(svc.get_chunk_background(10, 0))  # subtitle job joins
        await settle()
        fg.cancel()
        await settle()
        src.gate.set()
        return await bg

    assert run(go()) == chunk(0) and len(src.calls) == 1


def test_genuine_failure_reaches_every_waiter():
    src = GatedSource()
    src.fail_at_offset = 0
    svc = make_service(source=src)

    async def go():
        a = asyncio.ensure_future(svc.get_chunk(10, 1))
        b = asyncio.ensure_future(svc.get_chunk(10, 1))
        await settle()
        src.gate.set()
        return await asyncio.gather(a, b, return_exceptions=True)

    ra, rb = run(go())
    assert isinstance(ra, RuntimeError) and isinstance(rb, RuntimeError)
    assert svc._inflight == {} and svc.cache.get((10, 1)) is None


def test_prefetch_cancel_does_not_break_a_concurrent_reader():
    src = GatedSource()
    svc = make_service(source=src)

    async def go():
        pre = asyncio.ensure_future(svc.prefetch_edges(10, 2 * 1024 * 1024))
        await settle()
        reader = asyncio.ensure_future(svc.get_chunk(10, 0))
        await settle()
        pre.cancel()
        await settle()
        src.gate.set()
        return await reader

    assert run(go()) == chunk(0)


# --------------------------------------------------- (2)(3) real uvicorn server
class LiveServer:
    def __init__(self, app, http):
        import uvicorn

        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        cfg = uvicorn.Config(app, http=http, log_level="critical", lifespan="off")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.01)
        return self

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)


def raw_get(port, path, rng=None):
    """Read with a raw socket: returns (headers, body, clean) where clean
    means the server finished the message (Content-Length satisfied)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: t\r\n" + (f"Range: {rng}\r\n" if rng else "")
               + "Connection: close\r\n\r\n").encode())
    buf = b""
    while True:
        try:
            got = s.recv(65536)
        except (ConnectionResetError, socket.timeout):
            break
        if not got:
            break
        buf += got
    s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    length = next(int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length"))
    return head, body, len(body) == length


@pytest.mark.parametrize("http", ["h11", "httptools"])
def test_mid_body_fetch_failure_aborts_not_short_clean(http):
    class Broken(FakeSource):  # e.g. a Telegram DC connection dropping mid-file
        async def fetch(self, dc_id, location, offset, limit):
            if offset >= 2 * CHUNK_SIZE:
                raise ConnectionError("dc gone")
            return await super().fetch(dc_id, location, offset, limit)

    svc = make_service(source=Broken())
    app = build_stream_app(svc, lambda: KEY)
    with LiveServer(app, http) as srv:
        path = f"/s/10/{signing.sign(KEY, 10)}"
        head, body, clean = raw_get(srv.port, path)
        assert head.startswith(b"HTTP/1.1 200") and not clean and len(body) < SIZE
        with pytest.raises(httpx.HTTPError):  # a real client sees an error, not EOF
            httpx.get(f"http://127.0.0.1:{srv.port}{path}", timeout=5)
    assert svc.stats["aborted_responses"] >= 1 and svc.active == {}


@pytest.mark.parametrize("http", ["h11", "httptools"])
def test_short_body_is_never_sent_cleanly(http, monkeypatch):
    svc = make_service()
    real = svc.first_and_rest

    async def short(vid, start, end):  # a bug anywhere that loses the last piece
        first, rest = await real(vid, start, end)

        async def cut():
            pieces = [p async for p in rest]
            for p in pieces[:-1]:
                yield p
        return first, cut()

    monkeypatch.setattr(svc, "first_and_rest", short)
    app = build_stream_app(svc, lambda: KEY)
    with LiveServer(app, http) as srv:
        head, body, clean = raw_get(srv.port, f"/s/10/{signing.sign(KEY, 10)}", "bytes=0-")
        assert head.startswith(b"HTTP/1.1 206") and not clean
    assert svc.stats["aborted_responses"] == 1


def test_short_body_raises_at_asgi_level():
    svc = make_service()
    real = svc.first_and_rest

    async def short(vid, start, end):
        first, rest = await real(vid, start, end)

        async def nothing():
            await rest.aclose()
            if False:
                yield b""
        return first, nothing()

    svc.first_and_rest = short
    app = build_stream_app(svc, lambda: KEY)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            await c.get(f"/s/10/{signing.sign(KEY, 10)}")

    with pytest.raises(StreamError):
        run(go())
    assert svc.active == {}


def test_one_transient_mid_body_failure_is_retried_not_aborted():
    class Flaky(FakeSource):
        failed = False

        async def fetch(self, dc_id, location, offset, limit):
            if offset == 2 * CHUNK_SIZE and not Flaky.failed:
                Flaky.failed = True
                raise ConnectionError("dc blip")
            return await super().fetch(dc_id, location, offset, limit)

    svc = make_service(source=Flaky())

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_stream_app(svc, lambda: KEY)),
                                     base_url="http://t") as c:
            return await c.get(f"/s/10/{signing.sign(KEY, 10)}")

    r = run(go())
    assert r.status_code == 200 and r.content == CONTENT
    assert svc.stats["mid_body_retries"] == 1 and svc.stats["aborted_responses"] == 0
