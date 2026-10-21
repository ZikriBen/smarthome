"""TelegramChunkSource against a fake client: reuses the live client, <=2 senders
per DC, exported auth for foreign DCs, GetFile requests are aligned."""

import asyncio
from types import SimpleNamespace

import app.stream.telegram_source as ts
from telethon.tl.functions.auth import ExportAuthorizationRequest
from telethon.tl.functions.upload import GetFileRequest


class FakeSender:
    created = []

    def __init__(self, auth_key, loggers=None):
        self.auth_key = auth_key or "imported-key"
        self.sent = []
        self.connected = False
        FakeSender.created.append(self)

    async def connect(self, connection):
        self.connected = True

    async def send(self, request):
        self.sent.append(request)

    async def disconnect(self):
        self.connected = False


class FakeClient:
    def __init__(self):
        self.session = SimpleNamespace(dc_id=2, auth_key="home-key")
        self._log = None
        self._proxy = None
        self._init_request = SimpleNamespace(query=None)
        self.calls = []
        self.invoked = []
        self.fail_next = None

    async def _get_dc(self, dc_id):
        return SimpleNamespace(ip_address="127.0.0.1", port=443, id=dc_id)

    def _connection(self, *a, **k):
        return object()

    async def __call__(self, request):
        self.invoked.append(request)
        return SimpleNamespace(id=1, bytes=b"auth")

    async def _call(self, sender, request):
        self.calls.append((sender, request))
        if self.fail_next:
            exc, self.fail_next = self.fail_next, None
            raise exc
        return SimpleNamespace(bytes=b"x" * request.limit)


def test_source(monkeypatch):
    monkeypatch.setattr(ts, "MTProtoSender", FakeSender)
    FakeSender.created = []
    client = FakeClient()
    src = ts.TelegramChunkSource(client, connections=5)
    assert src.connections == 2  # hard cap

    async def go():
        for i in range(4):
            await src.fetch(2, "loc", i * 524288, 524288)
        await src.fetch(4, "loc", 0, 524288)  # foreign DC
        client.fail_next = ConnectionError("boom")
        try:
            await src.fetch(4, "loc", 0, 524288)
        except ConnectionError:
            pass
        await src.close()

    asyncio.run(go())
    home = [s for s in FakeSender.created if s.auth_key == "home-key"]
    assert len(home) == 2  # home DC reuses the live session's auth key, no new session
    used = [c[0] for c in client.calls[:4]]
    assert used[0] is not used[1] and used[0] is used[2]  # round robin
    assert all(isinstance(c[1], GetFileRequest) and c[1].offset % 524288 == 0 for c in client.calls)
    assert sum(isinstance(r, ExportAuthorizationRequest) for r in client.invoked) == 1
    assert all(not s.connected for s in FakeSender.created)
    assert 4 not in src._pools
