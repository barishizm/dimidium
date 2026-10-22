"""In-memory sliding-window rate limiter (one process, so no shared store is needed)."""

import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """Allows at most `limit` hits per key within any `window` seconds."""

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record a hit for `key`; False (and not recorded) if it is over the limit."""
        now = self._clock()
        self._prune(now)
        hits = self._hits.setdefault(key, deque())
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        for key in list(self._hits):
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if not hits:
                del self._hits[key]

    def __len__(self) -> int:
        return len(self._hits)
