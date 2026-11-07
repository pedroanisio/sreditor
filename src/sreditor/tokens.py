"""Personal API tokens for agents (rule agentic.tokens.scoped).

A token acts as its user, narrowed to its scopes: it never grants more than the user's role.
The secret is shown once at creation; the database keeps only its hash.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreditor.database import Database, parse, stamp
from sreditor.members import Permission
from sreditor.security import hash_token
from sreditor.users import User, UserStatus, UserStore

TOKEN_PREFIX = "sre_"
# Characters of the secret kept in clear, so a user can tell their tokens apart.
VISIBLE_CHARACTERS = len(TOKEN_PREFIX) + 6
DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 365


class Scope(StrEnum):
    """What an API token may do, on top of its user's role."""

    CHANNELS_READ = "channels:read"
    CHANNELS_WRITE = "channels:write"
    CHANNELS_DELETE = "channels:delete"
    EDITORIAL_WRITE = "editorial:write"
    MEMBERS_WRITE = "members:write"
    CONTENT_WRITE = "content:write"
    WAIVERS_READ = "waivers:read"


# The scope a token needs, on top of the user's role, for each channel permission.
SCOPE_FOR: dict[Permission, Scope] = {
    Permission.READ: Scope.CHANNELS_READ,
    Permission.WRITE_CONTENT: Scope.CONTENT_WRITE,
    Permission.WRITE_EDITORIAL: Scope.EDITORIAL_WRITE,
    Permission.READ_WAIVERS: Scope.WAIVERS_READ,
    Permission.WRITE_CHANNEL: Scope.CHANNELS_WRITE,
    Permission.MANAGE_MEMBERS: Scope.MEMBERS_WRITE,
    Permission.MANAGE_OWNERS: Scope.MEMBERS_WRITE,
    Permission.DELETE_CHANNEL: Scope.CHANNELS_DELETE,
}

TokenName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class ApiToken(BaseModel):
    """A token's record. The secret itself is never stored or returned again."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID
    name: str
    prefix: str
    scopes: list[Scope]
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    def allows(self, scope: Scope) -> bool:
        """Whether the token carries the scope."""
        return scope in self.scopes


class TokenInput(BaseModel):
    """A new token's name, scopes and lifetime."""

    model_config = ConfigDict(frozen=True)

    name: TokenName
    scopes: Annotated[list[Scope], Field(min_length=1)]
    expires_in_days: int = Field(default=DEFAULT_TTL_DAYS, ge=1, le=MAX_TTL_DAYS)


class NewToken(BaseModel):
    """Returned once, at creation: `secret` cannot be retrieved later."""

    model_config = ConfigDict(frozen=True)

    token: ApiToken
    secret: str


def is_api_token(credential: str) -> bool:
    """Whether a credential is an API token rather than a session."""
    return credential.startswith(TOKEN_PREFIX)


def to_token(row: sqlite3.Row) -> ApiToken:
    """An ApiToken from its database row."""
    return ApiToken(
        id=UUID(row["id"]),
        user_id=UUID(row["user_id"]),
        name=row["name"],
        prefix=row["prefix"],
        scopes=[Scope(s) for s in row["scopes"].split()],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        last_used_at=parse(row["last_used_at"]),
        revoked_at=parse(row["revoked_at"]),
    )


class TokenStore:
    """API tokens in the database, kept only as hashes."""

    def __init__(self, database: Database, users: UserStore) -> None:
        self.database = database
        self.users = users

    def create(self, user_id: UUID, data: TokenInput) -> NewToken:
        """Create a token; its secret is in the result and never stored."""
        secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = self.database.clock().replace(microsecond=0)
        token = ApiToken(
            id=uuid4(),
            user_id=user_id,
            name=data.name,
            prefix=secret[:VISIBLE_CHARACTERS],
            scopes=sorted(set(data.scopes)),
            created_at=now,
            expires_at=now + timedelta(days=data.expires_in_days),
        )
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO api_tokens (id, user_id, name, token_hash, prefix, scopes,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(token.id),
                    str(user_id),
                    token.name,
                    hash_token(secret),
                    token.prefix,
                    " ".join(token.scopes),
                    stamp(token.created_at),
                    stamp(token.expires_at),
                ),
            )
        return NewToken(token=token, secret=secret)

    def list(
        self, user_id: UUID, after: str | None = None, limit: int | None = None
    ) -> list[ApiToken]:
        """The user's tokens, newest first; `after` is the id of the last token already seen."""
        query = "SELECT * FROM api_tokens WHERE user_id = ?"
        params: list[str | int] = [str(user_id)]
        if after is not None:
            query += " AND (created_at, id) < (SELECT created_at, id FROM api_tokens WHERE id = ?)"
            params.append(after)
        query += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [to_token(row) for row in rows]

    def revoke(self, user_id: UUID, token_id: UUID) -> bool:
        """Revoke one of the user's tokens. False when the user has no such token."""
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE api_tokens SET revoked_at = COALESCE(revoked_at, ?)"
                " WHERE id = ? AND user_id = ? RETURNING id",
                (self.database.now(), str(token_id), str(user_id)),
            ).fetchone()
        return row is not None

    def authenticate(self, secret: str) -> tuple[User, ApiToken] | None:
        """The active user and live token behind a secret, or None."""
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?"
                " AND revoked_at IS NULL AND expires_at > ? RETURNING *",
                (now, hash_token(secret), now),
            ).fetchone()
        if row is None:
            return None
        token = to_token(row)
        user = self.users.get(token.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            return None
        return user, token
