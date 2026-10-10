"""A minimal in-memory, per-client sliding-window rate limiter.

Suitable for a single-process local deployment only. For multiple workers or hosts,
replace it with a shared store (e.g. Redis) or rate limiting at a reverse proxy.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.core.errors import RateLimitExceededError


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Record a request for `key`, raising if the limit is exceeded. 0 disables limiting."""
        if self.max_requests <= 0:
            return
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window_seconds:
                hits.popleft()
            if len(hits) >= self.max_requests:
                retry_after = max(1, int(self.window_seconds - (now - hits[0])) + 1)
                raise RateLimitExceededError(
                    "Too many requests. Please slow down.", headers={"Retry-After": str(retry_after)}
                )
            hits.append(now)
