"""Raw upload.getFile chunk source on the browser's EXISTING Telethon client.

No new session or client is created: extra MTProto senders are opened by the
already-running client (same process, same session object), exactly like
telegram-downloader's fast_download.SenderPool, which this is ported from
(telegram-downloader/app/fast_download.py L26-114, a copy, not a shared module:
moving it into shared/media_common would also force a downloader rebuild;
deferred to phase 2, see DESIGN §1).
"""

import asyncio
import itertools

from telethon import TelegramClient
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (
    ExportAuthorizationRequest,
    ImportAuthorizationRequest,
)
from telethon.tl.functions.upload import GetFileRequest


class _DcPool:
    """Ported from fast_download.SenderPool (sequential creation, exported auth for foreign DCs)."""

    def __init__(self, client: TelegramClient, dc_id: int):
        self.client = client
        self.dc_id = dc_id
        self.auth_key = client.session.auth_key if client.session.dc_id == dc_id else None
        self.senders: list[MTProtoSender] = []

    async def create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        connection = self.client._connection(
            dc.ip_address, dc.port, dc.id, loggers=self.client._log, proxy=self.client._proxy
        )
        await sender.connect(connection)
        if self.auth_key is None:
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            init_request = self.client._init_request
            init_request.query = ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
            await sender.send(InvokeWithLayerRequest(LAYER, init_request))
            self.auth_key = sender.auth_key
        self.senders.append(sender)
        return sender

    async def close(self) -> None:
        await asyncio.gather(*[s.disconnect() for s in self.senders], return_exceptions=True)
        self.senders.clear()


class TelegramChunkSource:
    """fetch(dc_id, location, offset, limit) -> bytes, over <= ``connections`` senders per DC."""

    def __init__(self, client: TelegramClient, connections: int = 2):
        self.client = client
        self.connections = max(1, min(int(connections), 2))  # PoC hard cap: 2
        self._pools: dict[int, _DcPool] = {}
        self._rr: dict[int, itertools.cycle] = {}
        self._lock = asyncio.Lock()

    async def _senders(self, dc_id: int) -> itertools.cycle:
        async with self._lock:
            pool = self._pools.get(dc_id)
            if pool is None or not pool.senders:
                pool = _DcPool(self.client, dc_id)
                try:
                    for _ in range(self.connections):
                        await pool.create_sender()
                except Exception:
                    await pool.close()
                    raise
                self._pools[dc_id] = pool
                self._rr[dc_id] = itertools.cycle(list(pool.senders))
            return self._rr[dc_id]

    async def fetch(self, dc_id: int, location, offset: int, limit: int) -> bytes:
        sender = next(await self._senders(dc_id))
        try:
            result = await self.client._call(
                sender, GetFileRequest(location=location, offset=offset, limit=limit)
            )
        except (ConnectionError, OSError):
            await self.reset(dc_id)
            raise
        return result.bytes

    async def reset(self, dc_id: int) -> None:
        async with self._lock:
            pool = self._pools.pop(dc_id, None)
            self._rr.pop(dc_id, None)
        if pool:
            await pool.close()

    async def close(self) -> None:
        for dc_id in list(self._pools):
            await self.reset(dc_id)
