"""Who the caller is: their user, the API token they act through and their role on each channel.

Shared by the MCP tool whoami (rule agentic.mcp). Reading it needs what listing channels needs,
channels:read, since the answer includes the channels the token reaches.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

from sreditor import operations
from sreditor.deps import Services
from sreditor.field_types import ChannelSlug
from sreditor.members import Role
from sreditor.pagination import Page
from sreditor.tokens import ApiToken, Scope
from sreditor.users import User


class TokenSummary(BaseModel):
    """The API token behind a request: what it may do and until when, never its secret."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    scopes: list[Scope]
    channels: list[ChannelSlug] | None
    created_at: datetime
    expires_at: datetime


class ChannelRole(BaseModel):
    """A channel the caller reaches and the role their user holds in it."""

    model_config = ConfigDict(frozen=True)

    slug: ChannelSlug
    name: str
    role: Role | None


class WhoAmI(BaseModel):
    """The caller's user, their token and a page of the channels they reach."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    display_name: str
    email: EmailStr
    is_admin: bool
    token: TokenSummary
    channels: Page[ChannelRole]


def summarize(token: ApiToken) -> TokenSummary:
    """The part of a token's record an agent may see about itself."""
    return TokenSummary(
        id=token.id,
        name=token.name,
        scopes=token.scopes,
        channels=token.channels,
        created_at=token.created_at,
        expires_at=token.expires_at,
    )


def who_am_i(app: Services, user: User, token: ApiToken, cursor: str | None, limit: int) -> WhoAmI:
    """The caller's identity and a page of their channels; admins who are not members have no role."""
    reached = operations.list_channels(app, user, cursor, limit, token)
    roles = app.members.roles_of(user.id)
    entries = [
        ChannelRole(slug=channel.slug, name=channel.name, role=roles.get(channel.slug))
        for channel in reached.items
    ]
    return WhoAmI(
        user_id=user.id,
        display_name=user.display_name,
        email=user.email,
        is_admin=user.is_admin,
        token=summarize(token),
        channels=Page[ChannelRole](items=entries, next_cursor=reached.next_cursor),
    )
