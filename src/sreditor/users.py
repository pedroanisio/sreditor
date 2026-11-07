"""Users, their sessions and their one-time email tokens."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints

from sreditor.database import Database, parse, stamp
from sreditor.security import hash_token, new_token

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Password = Annotated[str, StringConstraints(min_length=12, max_length=256)]


class UserStatus(StrEnum):
    """Where an account is in its life: awaiting verification, active or disabled."""

    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class TokenPurpose(StrEnum):
    """What a one-time email token is for."""

    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"


class User(BaseModel):
    """An account, without its password."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    email: EmailStr
    display_name: DisplayName
    status: UserStatus = UserStatus.PENDING
    is_admin: bool = False
    created_at: datetime
    email_verified_at: datetime | None = None


class UserExists(ValueError):
    """Another user already has that email."""


def normalize_email(email: str) -> str:
    """The email trimmed and lowercased, so one address is one account."""
    return email.strip().lower()


def to_user(row: sqlite3.Row) -> User:
    """A User from its database row."""
    return User(
        id=UUID(row["id"]),
        email=row["email"],
        display_name=row["display_name"],
        status=UserStatus(row["status"]),
        is_admin=bool(row["is_admin"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        email_verified_at=parse(row["email_verified_at"]),
    )


class UserStore:
    """Users, their one-time tokens and sessions in the database."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        email: str,
        display_name: str,
        password_hash: str,
        status: UserStatus = UserStatus.PENDING,
        is_admin: bool = False,
    ) -> User:
        """Create a pending user."""
        now = self.database.now()
        user_id = uuid4()
        try:
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO users (id, email, display_name, password_hash, status, is_admin,"
                    " created_at, email_verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(user_id),
                        normalize_email(email),
                        display_name,
                        password_hash,
                        status.value,
                        int(is_admin),
                        now,
                        now if status is UserStatus.ACTIVE else None,
                    ),
                )
        except sqlite3.IntegrityError as duplicate:
            raise UserExists(email) from duplicate
        user = self.get(user_id)
        assert user is not None
        return user

    def get(self, user_id: UUID) -> User | None:
        """The user with this id, or None."""
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (str(user_id),)).fetchone()
        return to_user(row) if row else None

    def credentials(self, email: str) -> tuple[User, str] | None:
        """The user with that email and their password hash."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
            ).fetchone()
        return (to_user(row), row["password_hash"]) if row else None

    def password_hash(self, user_id: UUID) -> str | None:
        """The user's password hash, or None when there is no such user."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = ?", (str(user_id),)
            ).fetchone()
        return row["password_hash"] if row else None

    def rename(self, user_id: UUID, display_name: str) -> User:
        """Change the display name and return the updated user."""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE users SET display_name = ? WHERE id = ?", (display_name, str(user_id))
            )
        user = self.get(user_id)
        assert user is not None
        return user

    def set_password(self, user_id: UUID, password_hash: str) -> None:
        """Change the password and sign the user out everywhere."""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, str(user_id))
            )
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (str(user_id),))

    def activate(self, user_id: UUID) -> None:
        """Mark the user active once their email is verified."""
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE users SET status = ?, email_verified_at = COALESCE(email_verified_at, ?)"
                " WHERE id = ? AND status = ?",
                (
                    UserStatus.ACTIVE.value,
                    self.database.now(),
                    str(user_id),
                    UserStatus.PENDING.value,
                ),
            )

    def issue_token(self, user_id: UUID, purpose: TokenPurpose, ttl: timedelta) -> str:
        """A one-time token for an email link; only its hash is stored."""
        token = new_token()
        expires = stamp(self.database.clock() + ttl)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO user_tokens (token_hash, user_id, purpose, expires_at) VALUES (?, ?, ?, ?)",
                (hash_token(token), str(user_id), purpose.value, expires),
            )
        return token

    def consume_token(self, token: str, purpose: TokenPurpose) -> UUID | None:
        """Use a one-time token. Returns its user, or None if it is unknown, used or expired."""
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE user_tokens SET used_at = ? WHERE token_hash = ? AND purpose = ?"
                " AND used_at IS NULL AND expires_at > ? RETURNING user_id",
                (now, hash_token(token), purpose.value, now),
            ).fetchone()
        return UUID(row["user_id"]) if row else None

    def create_session(self, user_id: UUID, ttl: timedelta, user_agent: str | None) -> str:
        """Start a session and return its token; only its hash is stored."""
        token = new_token()
        now = self.database.clock()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_used_at,"
                " user_agent) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    hash_token(token),
                    str(user_id),
                    stamp(now),
                    stamp(now + ttl),
                    stamp(now),
                    user_agent,
                ),
            )
        return token

    def session_user(self, token: str) -> User | None:
        """The active user a live session belongs to."""
        now = self.database.now()
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE sessions SET last_used_at = ? WHERE token_hash = ? AND expires_at > ?"
                " RETURNING user_id",
                (now, hash_token(token), now),
            ).fetchone()
        if row is None:
            return None
        user = self.get(UUID(row["user_id"]))
        return user if user is not None and user.status is UserStatus.ACTIVE else None

    def delete_session(self, token: str) -> None:
        """End the session with this token, if there is one."""
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
