"""Third-party media operations shared by the REST routes, the MCP tools and the pages.

Reading needs the read permission. Adding and editing items, their copies and their uses is
content work, open to contributors and the content:write scope, like sources. Removing an item
from the library takes it away from the whole channel, so it needs a role that may change the
channel, like removing a library entry.
"""

from __future__ import annotations

from typing import BinaryIO
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number, episode_access
from sreditor.external_media import (
    EpisodeCredits,
    ExternalKind,
    ExternalMedia,
    ExternalMediaInput,
    MediaUse,
    MediaUseInput,
    NewCopy,
    check_fits,
    episode_credits,
)
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, encode_cursor


class ExternalMediaOperation:
    """stmt operation names an agent's third-party media writes are gated on."""

    CREATE = "external_media.create"
    WRITE = "external_media.write"
    DELETE = "external_media.delete"
    COPY_WRITE = "external_media_copy.write"
    USE_CREATE = "external_media_use.create"
    USE_WRITE = "external_media_use.write"
    USE_DELETE = "external_media_use.delete"


class ItemChange(BaseModel):
    """Which item a change targets and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    media_id: UUID
    expected_revision: int | None = None


class UseChange(BaseModel):
    """Which use of which episode a change targets, and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    number: int
    use_id: UUID
    expected_revision: int | None = None


class ItemQuery(BaseModel):
    """Which page of the library to list, and of which kind."""

    model_config = ConfigDict(frozen=True)

    kind: ExternalKind | None = None
    cursor: str | None = None
    limit: int


class UseQuery(BaseModel):
    """Which page of an episode's uses to list."""

    model_config = ConfigDict(frozen=True)

    number: int
    cursor: str | None = None
    limit: int


def library_access(access: ChannelAccess, permission: Permission) -> str:
    """The channel's slug once the permission is checked."""
    access.require(permission)
    return access.channel.slug


def list_items(app: Services, access: ChannelAccess, query: ItemQuery) -> Page[ExternalMedia]:
    """The channel's third-party items, of one kind or all, in the order they were added."""
    slug = library_access(access, Permission.READ)
    rows = app.external_media.list(slug, after_number(query.cursor), query.limit + 1, query.kind)
    items = rows[: query.limit]
    has_more = len(rows) > query.limit
    cursor = encode_cursor(str(app.external_media.position(items[-1].id))) if has_more else None
    return Page[ExternalMedia](items=items, next_cursor=cursor)


def get_item(app: Services, access: ChannelAccess, media_id: UUID) -> ExternalMedia:
    """One item of the channel's library."""
    return app.external_media.get(library_access(access, Permission.READ), media_id)


def item_episodes(app: Services, access: ChannelAccess, media_id: UUID) -> list[int]:
    """The numbers of the episodes that use an item."""
    item = get_item(app, access, media_id)
    return app.external_media.episodes_of(item.id)


def create_item(app: Services, access: ChannelAccess, data: ExternalMediaInput) -> ExternalMedia:
    """Add an item to the channel's library."""
    slug = library_access(access, Permission.WRITE_CONTENT)
    return gated(
        app, access, ExternalMediaOperation.CREATE, lambda: app.external_media.create(slug, data)
    )


def replace_item(
    app: Services, access: ChannelAccess, change: ItemChange, data: ExternalMediaInput
) -> ExternalMedia:
    """Replace an item's details; its copy stays."""
    slug = library_access(access, Permission.WRITE_CONTENT)
    item = app.external_media.get(slug, change.media_id)
    return gated(
        app,
        access,
        ExternalMediaOperation.WRITE,
        lambda: app.external_media.replace(item, data, change.expected_revision),
    )


def delete_item(app: Services, access: ChannelAccess, change: ItemChange) -> None:
    """Remove an item from the library; refused while episodes use it."""
    slug = library_access(access, Permission.WRITE_CHANNEL)
    item = app.external_media.get(slug, change.media_id)
    gated(
        app,
        access,
        ExternalMediaOperation.DELETE,
        lambda: app.external_media.delete(item, change.expected_revision),
    )


def attach_copy(
    app: Services, access: ChannelAccess, change: ItemChange, source: BinaryIO
) -> ExternalMedia:
    """Check and store a copy of an item, replacing any earlier one; the gate decides first."""
    slug = library_access(access, Permission.WRITE_CONTENT)
    item = app.external_media.get(slug, change.media_id)
    new = NewCopy(item=item, expected_revision=change.expected_revision)
    return gated(
        app,
        access,
        ExternalMediaOperation.COPY_WRITE,
        lambda: app.external_media.attach(new, source),
    )


def detach_copy(app: Services, access: ChannelAccess, change: ItemChange) -> ExternalMedia:
    """Take the copy off an item."""
    slug = library_access(access, Permission.WRITE_CONTENT)
    item = app.external_media.get(slug, change.media_id)
    return gated(
        app,
        access,
        ExternalMediaOperation.COPY_WRITE,
        lambda: app.external_media.detach(item, change.expected_revision),
    )


def list_uses(app: Services, access: ChannelAccess, query: UseQuery) -> Page[MediaUse]:
    """An episode's uses of third-party items, in the order they were added."""
    slug = episode_access(app, access, query.number, Permission.READ)
    rows = app.external_media.uses(slug, query.number, after_number(query.cursor), query.limit + 1)
    items = rows[: query.limit]
    has_more = len(rows) > query.limit
    position = app.external_media.use_position
    cursor = encode_cursor(str(position(items[-1].id))) if has_more else None
    return Page[MediaUse](items=items, next_cursor=cursor)


def get_use(app: Services, access: ChannelAccess, number: int, use_id: UUID) -> MediaUse:
    """One use of the episode."""
    slug = episode_access(app, access, number, Permission.READ)
    return app.external_media.use(slug, number, use_id)


def add_use(app: Services, access: ChannelAccess, number: int, data: MediaUseInput) -> MediaUse:
    """Record a use of one of the channel's items in the episode; UseRefused when it does not fit."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    check_fits(app.external_media.get(slug, data.media_id), data)
    return gated(
        app,
        access,
        ExternalMediaOperation.USE_CREATE,
        lambda: app.external_media.add_use(slug, number, data),
    )


def replace_use(
    app: Services, access: ChannelAccess, change: UseChange, data: MediaUseInput
) -> MediaUse:
    """Replace a use's details, possibly pointing it at another of the channel's items."""
    slug = episode_access(app, access, change.number, Permission.WRITE_CONTENT)
    use = app.external_media.use(slug, change.number, change.use_id)
    check_fits(app.external_media.get(slug, data.media_id), data)
    return gated(
        app,
        access,
        ExternalMediaOperation.USE_WRITE,
        lambda: app.external_media.replace_use(use, data, change.expected_revision),
    )


def remove_use(app: Services, access: ChannelAccess, change: UseChange) -> None:
    """Take a use out of the episode; the item stays in the library."""
    slug = episode_access(app, access, change.number, Permission.WRITE_CONTENT)
    use = app.external_media.use(slug, change.number, change.use_id)
    gated(
        app,
        access,
        ExternalMediaOperation.USE_DELETE,
        lambda: app.external_media.delete_use(use, change.expected_revision),
    )


def get_credits(app: Services, access: ChannelAccess, number: int) -> EpisodeCredits:
    """The credits the episode owes for its third-party media, and its rights warnings."""
    slug = episode_access(app, access, number, Permission.READ)
    uses = app.external_media.all_uses(slug, number)
    items = app.external_media.items_of(slug, {use.media_id for use in uses})
    return episode_credits(uses, items)
