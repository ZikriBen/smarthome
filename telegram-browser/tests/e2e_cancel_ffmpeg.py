"""End-to-end check of the shared-fetch cancel fix with a real MKV and ffmpeg.

    python -m tests.e2e_cancel_ffmpeg sample.mkv [--stream 0:s:1] [--rounds N]

Serves the file through the REAL streamer (StreamService + build_stream_app
under uvicorn); only the Telegram layer is mocked (in-memory bytes with a
per-request latency). While ffmpeg extracts one subtitle track over HTTP
(exactly what Jellyfin does for a .strm), a disruptor keeps opening range
requests on the chunks ffmpeg is currently fetching and drops them after a
few KB, i.e. the browser player seeking / reconnecting. Prints the extracted
cue count vs. the direct-from-file count; exit 1 on mismatch.
"""
import argparse
import asyncio
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.stream import signing
from app.stream.cache import ChunkCache
from app.stream.ranges import CHUNK_SIZE
from app.stream.server import build_stream_app
from app.stream.service import StreamService

KEY = b"e" * 32
VID = 77


class FileSource:
    def __init__(self, data: bytes, latency: float):
        self.data, self.latency, self.calls = data, latency, 0

    async def fetch(self, dc_id, location, offset, limit):
        self.calls += 1
        await asyncio.sleep(self.latency * (0.5 + random.random()))
        return self.data[offset:offset + limit]


def cues(path: Path) -> int:
    return path.read_text(encoding="utf-8", errors="replace").count("-->")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mkv")
    ap.add_argument("--stream", default="0:s:1")
    ap.add_argument("--latency", type=float, default=0.03)
    ap.add_argument("--cache-mb", type=int, default=2)
    args = ap.parse_args()
    data = Path(args.mkv).read_bytes()
    tmp = Path(tempfile.mkdtemp())
    ref = tmp / "direct.srt"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", args.mkv, "-map", args.stream, ref], check=True)

    logs = []
    src = FileSource(data, args.latency)
    svc = StreamService(
        catalog_lookup=lambda v: {"message_id": VID, "filename": "x.mkv", "mime_type": "video/x-matroska",
                                  "size": len(data)} if v == VID else None,
        fetch_message=lambda v: _msg(len(data)),
        source=src, cache=ChunkCache(args.cache_mb * 1024 * 1024),
        extract=lambda m: (2, "loc", m.file.size), log=logs.append,
    )
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_stream_app(svc, lambda: KEY), log_level="critical",
                                           lifespan="off", http="httptools"))
    th = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.01)
    url = f"http://127.0.0.1:{port}/s/{VID}/{signing.sign(KEY, VID)}"

    stop = threading.Event()
    disrupted = [0]

    def disruptor():
        with httpx.Client(timeout=10) as c:
            while not stop.is_set():
                keys = [k for k in list(svc._inflight) if k[0] == VID]
                if not keys:
                    time.sleep(0.002)
                    continue
                idx = random.choice(keys)[1]
                start = idx * CHUNK_SIZE + random.randrange(0, CHUNK_SIZE // 2)
                try:
                    with c.stream("GET", url, headers={"Range": f"bytes={start}-"}) as r:
                        for _ in r.iter_bytes(4096):
                            break  # drop the connection mid-chunk
                    disrupted[0] += 1
                except httpx.HTTPError:
                    pass

    ds = [threading.Thread(target=disruptor, daemon=True) for _ in range(3)]
    for d in ds:
        d.start()
    out = tmp / "via_stream.srt"
    t0 = time.time()
    proc = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", url, "-map", args.stream, out],
                          capture_output=True, text=True)
    secs = time.time() - t0
    stop.set()
    for d in ds:
        d.join(5)
    server.should_exit = True
    th.join(5)
    want, got = cues(ref), cues(out) if out.exists() else 0
    print(f"ffmpeg rc={proc.returncode} in {secs:.1f}s; cues direct={want} via-stream={got}; "
          f"disruptions={disrupted[0]} telegram_fetches={src.calls} "
          f"abandoned={svc.stats.get('fetches_abandoned')} aborted={svc.stats.get('aborted_responses')}")
    if proc.stderr.strip():
        print("ffmpeg stderr:", proc.stderr.strip()[-400:])
    errs = [m for m in logs if "abort" in m or "cancel" in m]
    if errs:
        print("streamer log:", errs[:5])
    return 0 if got == want else 1


async def _msg(size):
    return SimpleNamespace(media=SimpleNamespace(), file=SimpleNamespace(size=size))


if __name__ == "__main__":
    sys.exit(main())
