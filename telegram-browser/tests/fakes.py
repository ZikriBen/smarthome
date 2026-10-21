import asyncio
import os
from types import SimpleNamespace

from telethon.errors import FileReferenceExpiredError, FloodWaitError

from app.stream.cache import ChunkCache
from app.stream.service import StreamService

KEY = b"k" * 32
SIZE = 3 * 512 * 1024 + 12345  # 4 chunks, last one partial
CONTENT = os.urandom(SIZE)


def flood(seconds):
    return FloodWaitError(request=None, capture=seconds)


def fileref():
    return FileReferenceExpiredError(request=None)


class FakeSource:
    def __init__(self, content=CONTENT):
        self.content = content
        self.calls = []
        self.errors = []  # exceptions raised by the next calls, in order
        self.fail_at_offset = None

    async def fetch(self, dc_id, location, offset, limit):
        self.calls.append((dc_id, location, offset, limit))
        await asyncio.sleep(0)
        if self.errors:
            raise self.errors.pop(0)
        if self.fail_at_offset is not None and offset >= self.fail_at_offset:
            raise flood(999)
        return self.content[offset:offset + limit]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def message(size=SIZE, loc="loc-1"):
    return SimpleNamespace(media=SimpleNamespace(loc=loc), file=SimpleNamespace(size=size))


def make_service(*, rows=None, messages=None, source=None, clock=None, sleeps=None, cache_mb=8, **kw):
    rows = rows if rows is not None else {
        10: {"message_id": 10, "mime_type": "video/x-matroska", "size": SIZE},
        11: {"message_id": 11, "mime_type": "video/mp4", "size": SIZE},
        12: {"message_id": 12, "mime_type": "video/mp4", "size": SIZE},
        13: {"message_id": 13, "mime_type": "image/jpeg", "size": 10},
    }
    messages = messages if messages is not None else {10: [message()], 11: [message()], 12: [message()]}
    fetch_log = []

    async def fetch_message(vid):
        fetch_log.append(vid)
        seq = messages.get(vid)
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def fake_sleep(s):
        if sleeps is not None:
            sleeps.append(s)

    svc = StreamService(
        catalog_lookup=lambda vid: rows.get(vid),
        fetch_message=fetch_message,
        source=source or FakeSource(),
        cache=ChunkCache(cache_mb * 1024 * 1024),
        extract=lambda m: (2, m.media.loc, m.file.size),
        clock=clock or Clock(),
        sleep=fake_sleep,
        log=lambda m: None,
        **kw,
    )
    svc.fetch_log = fetch_log
    return svc
