from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from time import monotonic
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: float = 60.0, maxsize: int = 256):
        self.ttl_seconds = ttl_seconds
        self.maxsize = maxsize
        self._lock = Lock()
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any):
        now = monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def set(self, key: Any, value: Any) -> None:
        now = monotonic()
        with self._lock:
            self._items[key] = (now + self.ttl_seconds, value)
            self._items.move_to_end(key)
            self._prune_locked(now)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, (expires_at, _) in self._items.items() if expires_at <= now]
        for key in expired:
            self._items.pop(key, None)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)


store_resolution_cache = TTLCache(ttl_seconds=60.0, maxsize=128)
peer_store_cache = TTLCache(ttl_seconds=60.0, maxsize=128)


def clear_operator_caches() -> None:
    store_resolution_cache.clear()
    peer_store_cache.clear()
