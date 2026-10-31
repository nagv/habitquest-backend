"""Rate limits for sign-in and PIN attempts."""

import time
from collections import deque

from fastapi import HTTPException, Request


class RateLimiter:
    """A sliding window per key, in memory: enough for one instance, and it resets on restart."""

    def __init__(self, limit: int, seconds: float = 60) -> None:
        self.limit = limit
        self.seconds = seconds
        self.hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = self.hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.seconds:
            hits.popleft()
        if len(hits) >= self.limit:
            raise HTTPException(429, "Too many attempts. Please wait a minute and try again.")
        hits.append(now)


def client_address(request: Request) -> str:
    """The caller's address: Render's proxy puts it first in X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")
