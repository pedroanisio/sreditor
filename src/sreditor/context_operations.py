"""Context operations shared by the REST routes, the MCP tools and the pages (rule agentic.mcp).

Reading an item's context and links needs the read permission; writing answers and adding or
removing links is content work, open to contributors and the content:write scope.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.context import (
    ContextInput,
    ItemContext,
    ItemKind,
    ItemRef,
    Link,
    LinkDirection,
    LinkedItem,
    LinkInput,
)
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import after_number
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.pagination import Page, encode_cursor


class ContextOperation:
    """stmt operation names an agent's context writes are gated on."""

    WRITE = "item_context.write"
    LINK = "item_link.create"
    UNLINK = "item_link.delete"


class ContextChange(BaseModel):
    """Which item's context a write targets and the revision the caller last saw."""

    model_config = ConfigDict(frozen=True)

    item: ItemRef
    expected_revision: int | None = None


class LinkQuery(BaseModel):
    """Which page of an item's links, made by it (out) or to it (in)."""

    model_config = ConfigDict(frozen=True)

    item: ItemRef
    direction: LinkDirection = LinkDirection.OUT
    cursor: str | None = None
    limit: int


def item_access(
    app: Services, access: ChannelAccess, item: ItemRef, permission: Permission
) -> LinkedItem:
    """The channel's item once the permission is checked; ItemNotFound when there is none."""
    access.require(permission)
    return app.contexts.find(access.channel.slug, item)


class CatalogQuery(BaseModel):
    """Which page of the channel's linkable items, optionally of one kind."""

    model_config = ConfigDict(frozen=True)

    kind: ItemKind | None = None
    cursor: str | None = None
    limit: int


def catalog(app: Services, access: ChannelAccess, query: CatalogQuery) -> Page[LinkedItem]:
    """The channel's items that may be linked, kind by kind; the cursor is an opaque offset."""
    access.require(Permission.READ)
    skipped = after_number(query.cursor) or 0
    rows = app.contexts.catalog(access.channel.slug, query.kind, skipped, query.limit + 1)
    shown = rows[: query.limit]
    has_more = len(rows) > query.limit
    return Page[LinkedItem](
        items=shown,
        next_cursor=encode_cursor(str(skipped + len(shown))) if has_more else None,
    )


def get_context(app: Services, access: ChannelAccess, item: ItemRef) -> ItemContext:
    """The item's answers to the six questions, with the revision to send back."""
    found = item_access(app, access, item, Permission.READ)
    return app.contexts.context(access.channel.slug, found)


def write_context(
    app: Services, access: ChannelAccess, change: ContextChange, data: ContextInput
) -> ItemContext:
    """Replace the item's answers to the six questions."""
    found = item_access(app, access, change.item, Permission.WRITE_CONTENT)
    slug = access.channel.slug
    reference = ItemRef(kind=found.kind, id=found.id)
    return gated(
        app,
        access,
        ContextOperation.WRITE,
        lambda: app.contexts.write_context(slug, reference, data, change.expected_revision),
    )


def list_links(app: Services, access: ChannelAccess, query: LinkQuery) -> Page[Link]:
    """The links an item makes (out), or the items that mention it (in), oldest first."""
    item_access(app, access, query.item, Permission.READ)
    rows = app.contexts.links(
        access.channel.slug,
        query.item,
        query.direction,
        after_number(query.cursor),
        query.limit + 1,
    )
    shown = rows[: query.limit]
    has_more = len(rows) > query.limit and shown
    return Page[Link](
        items=[link for _, link in shown],
        next_cursor=encode_cursor(str(shown[-1][0])) if has_more else None,
    )


def add_link(app: Services, access: ChannelAccess, item: ItemRef, data: LinkInput) -> Link:
    """Link the item to another of the channel, answering one question about it."""
    source = item_access(app, access, item, Permission.WRITE_CONTENT)
    slug = access.channel.slug
    return gated(
        app, access, ContextOperation.LINK, lambda: app.contexts.add_link(slug, source, data)
    )


def get_link(app: Services, access: ChannelAccess, link_id: UUID) -> Link:
    """One link of the channel."""
    access.require(Permission.READ)
    return app.contexts.link(access.channel.slug, link_id)


def remove_link(app: Services, access: ChannelAccess, link_id: UUID) -> None:
    """Remove a link; neither item changes."""
    access.require(Permission.WRITE_CONTENT)
    slug = access.channel.slug
    app.contexts.link(slug, link_id)
    gated(app, access, ContextOperation.UNLINK, lambda: app.contexts.remove_link(slug, link_id))
