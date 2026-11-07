"""Channel roles, memberships and invitations."""

from __future__ import annotations

import builtins
import sqlite3
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, EmailStr

from sreditor.database import Database, parse, stamp
from sreditor.security import hash_token, new_token


class Role(StrEnum):
    """A member's role on a channel, from most to least power."""

    OWNER = "owner"
    EDITOR = "editor"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class Permission(StrEnum):
    """What a role may do on a channel."""

    READ = "read"
    WRITE_CONTENT = "write_content"
    WRITE_EDITORIAL = "write_editorial"
    READ_WAIVERS = "read_waivers"
    GENERATE_VOICE = "generate_voice"  # spends the account's ElevenLabs credits
    WRITE_CHANNEL = "write_channel"
    MANAGE_MEMBERS = "manage_members"
    MANAGE_OWNERS = "manage_owners"
    DELETE_CHANNEL = "delete_channel"


_VIEWER = frozenset({Permission.READ})
_CONTRIBUTOR = _VIEWER | {Permission.WRITE_CONTENT}
_EDITOR = _CONTRIBUTOR | {
    Permission.WRITE_EDITORIAL,
    Permission.READ_WAIVERS,
    Permission.GENERATE_VOICE,
    Permission.WRITE_CHANNEL,
    Permission.MANAGE_MEMBERS,
}
PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.CONTRIBUTOR: _CONTRIBUTOR,
    Role.EDITOR: _EDITOR,
    Role.OWNER: frozenset(Permission),
}

# Granting, changing or removing these roles needs MANAGE_OWNERS.
ELEVATED_ROLES = frozenset({Role.OWNER, Role.EDITOR})


def allows(role: Role | None, permission: Permission) -> bool:
    """Whether the role grants the permission; no role grants nothing."""
    return role is not None and permission in PERMISSIONS[role]


class Member(BaseModel):
    """A user's membership of a channel."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    email: EmailStr
    display_name: str
    role: Role
    added_by: UUID | None
    created_at: datetime


class Invitation(BaseModel):
    """An emailed offer to join a channel with a role."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    channel: str
    email: EmailStr
    role: Role
    invited_by: UUID
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None = None


class AlreadyMember(ValueError):
    """The user already belongs to the channel."""


def to_invitation(row: sqlite3.Row) -> Invitation:
    """An Invitation from its database row."""
    return Invitation(
        id=UUID(row["id"]),
        channel=row["channel_slug"],
        email=row["email"],
        role=Role(row["role"]),
        invited_by=UUID(row["invited_by"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        accepted_at=parse(row["accepted_at"]),
    )


class MemberStore:
    """Memberships and invitations in the database."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def role(self, slug: str, user_id: UUID) -> Role | None:
        """The user's role on the channel, or None when they are not a member."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT role FROM memberships WHERE channel_slug = ? AND user_id = ?",
                (slug, str(user_id)),
            ).fetchone()
        return Role(row["role"]) if row else None

    def roles_of(self, user_id: UUID) -> dict[str, Role]:
        """Each channel the user belongs to, with their role there."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT channel_slug, role FROM memberships WHERE user_id = ? ORDER BY channel_slug",
                (str(user_id),),
            ).fetchall()
        return {row["channel_slug"]: Role(row["role"]) for row in rows}

    def list(self, slug: str) -> list[Member]:
        """The channel's members, ordered by email."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT m.user_id, u.email, u.display_name, m.role, m.added_by, m.created_at"
                " FROM memberships m JOIN users u ON u.id = m.user_id"
                " WHERE m.channel_slug = ? ORDER BY u.email",
                (slug,),
            ).fetchall()
        return [
            Member(
                user_id=UUID(row["user_id"]),
                email=row["email"],
                display_name=row["display_name"],
                role=Role(row["role"]),
                added_by=UUID(row["added_by"]) if row["added_by"] else None,
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def owner_count(self, slug: str) -> int:
        """How many owners the channel has."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM memberships WHERE channel_slug = ? AND role = ?",
                (slug, Role.OWNER.value),
            ).fetchone()
        return int(row["n"])

    def set_role(self, slug: str, user_id: UUID, role: Role, current: Role) -> bool:
        """Change the role only while it is still `current`; False when it moved on."""
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE memberships SET role = ? WHERE channel_slug = ? AND user_id = ?"
                " AND role = ? RETURNING user_id",
                (role.value, slug, str(user_id), current.value),
            ).fetchone()
        return row is not None

    def remove(self, slug: str, user_id: UUID, current: Role) -> bool:
        """Remove the member only while their role is still `current`; False otherwise."""
        with self.database.connect() as connection:
            row = connection.execute(
                "DELETE FROM memberships WHERE channel_slug = ? AND user_id = ? AND role = ?"
                " RETURNING user_id",
                (slug, str(user_id), current.value),
            ).fetchone()
        return row is not None

    def invite(
        self, slug: str, email: str, role: Role, invited_by: UUID, ttl: timedelta
    ) -> tuple[Invitation, str]:
        """Store an invitation; returns it with the token for the email link."""
        token = new_token()
        now = self.database.clock()
        invitation = Invitation(
            id=uuid4(),
            channel=slug,
            email=email.strip().lower(),
            role=role,
            invited_by=invited_by,
            created_at=now.replace(microsecond=0),
            expires_at=(now + ttl).replace(microsecond=0),
        )
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO invitations (id, channel_slug, email, role, token_hash, invited_by,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(invitation.id),
                    slug,
                    invitation.email,
                    role.value,
                    hash_token(token),
                    str(invited_by),
                    stamp(invitation.created_at),
                    stamp(invitation.expires_at),
                ),
            )
        return invitation, token

    def pending_invitations(self, slug: str) -> builtins.list[Invitation]:
        """The channel's invitations that are neither accepted nor expired, newest first."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM invitations WHERE channel_slug = ? AND accepted_at IS NULL"
                " AND expires_at > ? ORDER BY created_at DESC, id",
                (slug, self.database.now()),
            ).fetchall()
        return [to_invitation(row) for row in rows]

    def revoke_invitation(self, slug: str, invitation_id: UUID) -> bool:
        """Delete a pending invitation, so its link stops working. False when there is none."""
        with self.database.connect() as connection:
            row = connection.execute(
                "DELETE FROM invitations WHERE id = ? AND channel_slug = ? AND accepted_at IS NULL"
                " RETURNING id",
                (str(invitation_id), slug),
            ).fetchone()
        return row is not None

    def open_invitation(self, token: str) -> Invitation | None:
        """An invitation that is neither accepted nor expired."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM invitations WHERE token_hash = ? AND accepted_at IS NULL"
                " AND expires_at > ?",
                (hash_token(token), self.database.now()),
            ).fetchone()
        return to_invitation(row) if row else None

    def accept(self, invitation: Invitation, user_id: UUID) -> None:
        """Add the member and close the invitation in one transaction."""
        now = self.database.now()
        try:
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO memberships (channel_slug, user_id, role, added_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        invitation.channel,
                        str(user_id),
                        invitation.role.value,
                        str(invitation.invited_by),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE invitations SET accepted_at = ? WHERE id = ?", (now, str(invitation.id))
                )
        except sqlite3.IntegrityError as duplicate:
            raise AlreadyMember(invitation.channel) from duplicate
