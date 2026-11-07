"""Passwords, opaque tokens and rate limiting."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


def new_token() -> str:
    """A random token for a session, an email link or an invitation."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are stored only as hashes, so a leaked database cannot be used to sign in."""
    return hashlib.sha256(token.encode()).hexdigest()


class Passwords:
    """Argon2id password hashing."""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self.hasher = hasher or PasswordHasher()
        self._dummy: str | None = None

    def hash(self, password: str) -> str:
        """The hash of a password, to store instead of the password."""
        return self.hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        """Check a password. With no hash, spend the same time and fail, to hide unknown emails."""
        if password_hash is None:
            if self._dummy is None:
                self._dummy = self.hasher.hash(new_token())
            password_hash = self._dummy
            password = ""
        try:
            return self.hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False


class RateLimiter:
    """Allow at most `limit` hits per key within a sliding window of `window` seconds."""

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)
        # Sync routes run on worker threads, so two hits for one key can arrive at once.
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Count a hit unless the key is already at its limit."""
        with self._lock:
            hits = self._recent(key)
            if len(hits) >= self.limit:
                return False
            hits.append(self.clock())
            return True

    def retry_after(self, key: str) -> float:
        """Seconds until the key may hit again: 0 while it is under its limit."""
        with self._lock:
            hits = self._recent(key)
            if len(hits) < self.limit:
                return 0.0
            return max(0.0, hits[0] + self.window - self.clock())

    def _recent(self, key: str) -> deque[float]:
        now = self.clock()
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits
