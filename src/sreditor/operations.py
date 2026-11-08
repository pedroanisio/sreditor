"""Channel operations shared by the REST routes and the MCP tools (rule agentic.mcp).

Writes made with an API token pass the stmt gate first and emit their AFTER event once they
succeed (rule agentic.gate), and record the token as their author (rule agentic.audit).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import status
from pydantic import BaseModel, ConfigDict

from sreditor.brand import BrandPatch, own_brand, with_brand, with_patch
from sreditor.channels import (
    BrandKit,
    Channel,
    ChannelIdentity,
    ChannelRename,
    Platform,
    PlatformAccount,
)
from sreditor.deps import ChannelAccess, ScopeMissing, Services, author
from sreditor.editorial import Editorial, Guideline, GuidelineInput
from sreditor.members import Permission
from sreditor.pagination import Page, decode_cursor, page
from sreditor.problems import ProblemError
from sreditor.repository import ChannelExists, ChannelNotFound
from sreditor.search import SearchHit
from sreditor.store import Owner
from sreditor.tokens import ApiToken, NewToken, Scope, TokenInput
from sreditor.users import User

ALLOW = "ALLOW"
TOKEN_OWNER = "token"


class Operation:
    """stmt operation names an agent's writes are gated on."""

    CHANNEL_CREATE = "channel.create"
    CHANNEL_WRITE = "channel.write"
    CHANNEL_DELETE = "channel.delete"
    EDITORIAL_WRITE = "editorial.write"
    EDITORIAL_RETIRE = "editorial.retire"


class Revised(BaseModel):
    """A channel together with the revision its ETag is built from."""

    model_config = ConfigDict(frozen=True)

    channel: Channel
    revision: int


def owner_of(user: User, token: ApiToken | None) -> Owner:
    """The stmt author of a write: the token when an agent made it, otherwise the user."""
    if token is not None:
        return Owner(type=TOKEN_OWNER, id=str(token.id))
    return author(user)


def gated[T](app: Services, access: ChannelAccess, operation: str, write: Callable[[], T]) -> T:
    """Run a write; with an API token, only after the stmt gate in the channel's scope allows it."""
    return gate_in(app, app.editorials.scope(access.channel.slug), access, operation, write)


def gate_in[T](
    app: Services, scope: str, access: ChannelAccess, operation: str, write: Callable[[], T]
) -> T:
    """Run `write`, but for an API token first ask the stmt gate and emit its AFTER event."""
    if access.token is None:
        return write()
    context: dict[str, Any] = {
        "channel": access.channel.slug,
        "user": str(access.user.id),
        "role": access.role.value if access.role else None,
        "token": str(access.token.id),
        "scopes": [str(scope) for scope in access.token.scopes],
    }
    gate_id = f"gate-{uuid4()}"
    decision = app.stmt.gate(scope, {"id": gate_id, "operation": operation, "context": context})
    if decision.decision != ALLOW:
        raise ProblemError(
            status.HTTP_403_FORBIDDEN,
            f"The rules for this channel do not allow {operation} ({decision.decision})",
            "gate-refused",
            decision=decision.decision,
            event_id=decision.event_id,
            blocks=decision.blocks,
        )
    result = write()
    app.stmt.emit(
        scope,
        {"id": f"after-{uuid4()}", "cause": gate_id, "operation": operation, "context": context},
    )
    return result


def require_account(channel: Channel, platform: Platform | None) -> None:
    """Refuse with 422 when the channel has no account on the platform; no platform passes."""
    if platform is not None and channel.account(platform) is None:
        raise ProblemError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Channel {channel.slug} has no {platform} account",
            "no-platform-account",
        )


LIMITED_TOKEN_CREATES = (
    "This token is limited to some channels, so it cannot create channels; "
    "create one with a token for all your channels"
)
NOT_YOUR_CHANNEL = "You are not a member of %(slug)s"


def channel_exists(app: Services, slug: str) -> bool:
    """Whether a channel with this slug exists."""
    try:
        app.channels.get(slug)
    except ChannelNotFound:
        return False
    return True


def create_token(app: Services, user: User, data: TokenInput) -> NewToken:
    """Create an API token; every channel it is limited to must be one the user belongs to."""
    if data.channels is not None:
        mine = set(app.members.roles_of(user.id))
        for slug in data.channels:
            if slug not in mine and not (user.is_admin and channel_exists(app, slug)):
                raise ProblemError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    NOT_YOUR_CHANNEL % {"slug": slug},
                    "validation",
                    errors=[
                        {
                            "location": ["body", "channels"],
                            "message": NOT_YOUR_CHANNEL % {"slug": slug},
                            "type": "not_a_member",
                        }
                    ],
                )
    return app.tokens.create(user.id, data)


def require_scope(token: ApiToken | None, scope: Scope) -> None:
    """Refuse with 403 a token that lacks the scope; sessions are not narrowed by scopes."""
    if token is not None and not token.allows(scope):
        raise ScopeMissing(scope)


def list_channels(
    app: Services, user: User, cursor: str | None, limit: int, token: ApiToken | None = None
) -> Page[Channel]:
    """The channels the user belongs to (every channel for an admin), ordered by slug.

    A token needs channels:read, as reading any one of those channels does, and sees only the
    channels it is limited to, if any.
    """
    require_scope(token, Scope.CHANNELS_READ)
    slugs = None if user.is_admin else list(app.members.roles_of(user.id))
    if token is not None and token.channels is not None:
        slugs = [slug for slug in slugs or token.channels if token.reaches(slug)]
    rows = app.channels.list(slugs, after=decode_cursor(cursor), limit=limit + 1)
    return page(rows, limit, lambda channel: channel.slug)


def create_channel(app: Services, user: User, token: ApiToken | None, channel: Channel) -> Channel:
    """Create a channel owned by the user; a token needs the channels:write scope.

    A token limited to some channels cannot create one: it could not reach what it made.
    """
    require_scope(token, Scope.CHANNELS_WRITE)
    if token is not None and token.is_limited:
        raise ProblemError(status.HTTP_403_FORBIDDEN, LIMITED_TOKEN_CREATES)
    try:
        app.channels.get(channel.slug)
    except ChannelNotFound:
        access = ChannelAccess(channel=channel, role=None, user=user, token=token)

        def create() -> Channel:
            app.editorials.register(channel)
            return app.channels.create(channel, owner=user.id)

        return gate_in(app, app.stmt.settings.scope, access, Operation.CHANNEL_CREATE, create)
    raise ChannelExists(channel.slug)


def replace_channel(
    app: Services, access: ChannelAccess, channel: Channel, expected_revision: int | None
) -> Revised:
    """Replace the whole channel document, optionally only if it is still at a revision."""
    access.require(Permission.WRITE_CHANNEL)
    if channel.slug != access.channel.slug:
        raise ProblemError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Slug in body and path differ", "slug-mismatch"
        )

    def replace() -> Revised:
        app.editorials.register(channel)
        return Revised(channel=channel, revision=app.channels.replace(channel, expected_revision))

    return gated(app, access, Operation.CHANNEL_WRITE, replace)


def rename_channel(
    app: Services, access: ChannelAccess, rename: ChannelRename, expected_revision: int | None
) -> Revised:
    """Change the channel's display name; the slug stays (rule project.brand)."""
    return change_identity(app, access, ChannelIdentity(name=rename.name), expected_revision)


def change_identity(
    app: Services, access: ChannelAccess, identity: ChannelIdentity, expected_revision: int | None
) -> Revised:
    """Change the channel's display name, language or both; the slug stays."""
    changes = identity.model_dump(exclude_none=True)
    return replace_channel(
        app, access, access.channel.model_copy(update=changes), expected_revision
    )


def put_account(
    app: Services, access: ChannelAccess, account: PlatformAccount, expected_revision: int | None
) -> Revised:
    """Add the channel's account on a platform, or replace it."""
    others = [a for a in access.channel.accounts if a.platform != account.platform]
    updated = access.channel.model_copy(update={"accounts": [*others, account]})
    return replace_channel(app, access, updated, expected_revision)


def delete_account(
    app: Services, access: ChannelAccess, platform: Platform, expected_revision: int | None
) -> Revised:
    """Remove the channel's account on a platform."""
    channel = access.channel
    if channel.account(platform) is None:
        raise ProblemError(
            status.HTTP_404_NOT_FOUND, f"Channel {channel.slug} has no {platform} account"
        )
    remaining = [a for a in channel.accounts if a.platform != platform]
    return replace_channel(
        app, access, channel.model_copy(update={"accounts": remaining}), expected_revision
    )


def delete_channel(app: Services, access: ChannelAccess, expected_revision: int | None) -> None:
    """Delete the channel record, its members and invitations; its Editorial stays in stmt."""
    access.require(Permission.DELETE_CHANNEL)
    slug = access.channel.slug
    gated(
        app, access, Operation.CHANNEL_DELETE, lambda: app.channels.delete(slug, expected_revision)
    )


def get_brand(access: ChannelAccess, platform: Platform | None) -> BrandKit:
    """The brand kit, with the platform's overrides applied when one is given."""
    access.require(Permission.READ)
    require_account(access.channel, platform)
    return access.channel.brand_for(platform)


def update_brand(
    app: Services,
    access: ChannelAccess,
    platform: Platform | None,
    patch: BrandPatch,
    expected_revision: int | None,
) -> Revised:
    """Change the colors and fonts of the channel's kit, or of one account's overrides."""
    access.require(Permission.WRITE_CHANNEL)
    require_account(access.channel, platform)
    kit = with_patch(own_brand(access.channel, platform), patch)
    updated = with_brand(access.channel, platform, kit)
    return replace_channel(app, access, updated, expected_revision)


def get_editorial(app: Services, access: ChannelAccess, platform: Platform | None) -> Editorial:
    """House rules, the channel's guidelines and, with a platform, its overrides."""
    access.require(Permission.READ)
    require_account(access.channel, platform)
    return app.editorials.editorial(access.channel, platform)


def write_guideline(
    app: Services,
    access: ChannelAccess,
    data: GuidelineInput,
    expected_revision: int | None = None,
) -> Guideline:
    """Set the purpose, voice or audience, or add a pillar or an avoid guideline.

    With `expected_revision`, purpose, voice and audience change only from that revision.
    """
    access.require(Permission.WRITE_EDITORIAL)
    require_account(access.channel, data.platform)
    owner = owner_of(access.user, access.token)
    return gated(
        app,
        access,
        Operation.EDITORIAL_WRITE,
        lambda: app.editorials.write(access.channel, data, owner, expected_revision),
    )


def retire_guideline(
    app: Services,
    access: ChannelAccess,
    statement_id: str,
    platform: Platform | None,
    expected_revision: int | None = None,
) -> None:
    """Retire one of the channel's guidelines, if it is still at `expected_revision`."""
    access.require(Permission.WRITE_EDITORIAL)
    require_account(access.channel, platform)
    owner = owner_of(access.user, access.token)
    gated(
        app,
        access,
        Operation.EDITORIAL_RETIRE,
        lambda: app.editorials.retire(
            access.channel, statement_id, platform, owner, expected_revision
        ),
    )


def search_channels(app: Services, channels: list[str], query: str, limit: int) -> list[SearchHit]:
    """Episodes and pitches in channels the caller already found the person belongs to."""
    return app.search.find(channels, query, limit)
