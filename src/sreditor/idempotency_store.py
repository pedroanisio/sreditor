"""Where Idempotency-Key reservations and their responses are kept (rule agentic.idempotency).

This is the store layer: it speaks SQL and knows nothing of HTTP (code.layers.separation);
sreditor.idempotency turns its errors into problem responses.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sreditor.database import Database, stamp

KEY_TTL = timedelta(hours=24)


class KeyReused(Exception):
    """The key was already used for a request with another method, path or body."""


class KeyInProgress(Exception):
    """The first request with the key has not finished yet."""


@dataclass(frozen=True)
class StoredResponse:
    """A response kept under an Idempotency-Key so a repeat of the request can replay it."""

    status_code: int
    body: Any


class IdempotencyStore:
    """Keeps Idempotency-Key reservations and their responses in the database."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def begin(self, user_id: UUID, key: str, request_hash: str) -> StoredResponse | None:
        """Reserve the key, or return the response stored for it.

        Raises KeyReused when the key was used for a different request, KeyInProgress while
        that request is still running.
        """
        now = self.database.clock()
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM idempotency_keys WHERE created_at <= ?", (stamp(now - KEY_TTL),)
            )
            try:
                connection.execute(
                    "INSERT INTO idempotency_keys (user_id, key, request_hash, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (str(user_id), key, request_hash, stamp(now)),
                )
                return None
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT request_hash, status_code, response_body FROM idempotency_keys"
                    " WHERE user_id = ? AND key = ?",
                    (str(user_id), key),
                ).fetchone()
        if row["request_hash"] != request_hash:
            raise KeyReused(key)
        if row["status_code"] is None:
            raise KeyInProgress(key)
        return StoredResponse(int(row["status_code"]), json.loads(row["response_body"]))

    def complete(self, user_id: UUID, key: str, response: StoredResponse) -> None:
        """Store the response under the reservation so later requests with the key replay it."""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE idempotency_keys SET status_code = ?, response_body = ?"
                " WHERE user_id = ? AND key = ?",
                (response.status_code, json.dumps(response.body), str(user_id), key),
            )

    def release(self, user_id: UUID, key: str) -> None:
        """Forget a reservation whose request failed, so the client can retry it."""
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM idempotency_keys WHERE user_id = ? AND key = ?", (str(user_id), key)
            )
