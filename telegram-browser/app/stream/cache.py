"""Bounded in-memory LRU cache of aligned 512 KiB chunks (no disk writes)."""

from collections import OrderedDict


class ChunkCache:
    def __init__(self, max_bytes: int):
        self.max_bytes = max(0, int(max_bytes))
        self._data: "OrderedDict[tuple[int, int], bytes]" = OrderedDict()
        self.bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, key):
        value = self._data.get(key)
        if value is None:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key, value: bytes) -> None:
        if self.max_bytes <= 0 or len(value) > self.max_bytes:
            return
        old = self._data.pop(key, None)
        if old is not None:
            self.bytes -= len(old)
        self._data[key] = value
        self.bytes += len(value)
        while self.bytes > self.max_bytes and self._data:
            _, evicted = self._data.popitem(last=False)
            self.bytes -= len(evicted)

    def __contains__(self, key) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def drop_variant(self, variant_id: int) -> None:
        for key in [k for k in self._data if k[0] == variant_id]:
            self.bytes -= len(self._data.pop(key))
