"""In-memory sliding-window rate limiter.

Correct only because the app runs as ONE process in ONE replica (Container App max replicas = 1,
uvicorn single worker). Scaling out would need a shared store (SQL table or Redis). The limits
that must survive a restart -- account lockout and the daily upload count -- live in SQL instead.
"""

from __future__ import annotations

import threading
import time
from collections import deque

_MAX_KEYS = 50_000  # memory guard: beyond this the oldest buckets are dropped


class Limiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limits: tuple[tuple[int, int], ...], now: float | None = None) -> bool:
        """Record one attempt for `key`; False if any (count, window) limit is already used up."""
        now = time.monotonic() if now is None else now
        longest = max(window for _, window in limits)
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - longest:
                q.popleft()
            for count, window in limits:
                if sum(1 for t in q if t > now - window) >= count:
                    return False
            q.append(now)
            if len(self._hits) > _MAX_KEYS:
                for stale in list(self._hits)[: len(self._hits) - _MAX_KEYS]:
                    del self._hits[stale]
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = Limiter()
