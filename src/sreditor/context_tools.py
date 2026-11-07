"""MCP tools for items' context and the links between them (rule agentic.mcp).

Task-sized tools (agentic.mcp.tools.outcomes): find the items a link can point to, read an
item's context with its links and mentions, write answers and links in one call, drop a link.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from sreditor import context_operations as contexts
from sreditor.context import (
    ContextInput,
    ItemContext,
    ItemKind,
    ItemNotFound,
    ItemRef,
    Link,
    LinkDirection,
    LinkedItem,
    LinkExists,
    LinkInput,
    LinkNotFound,
    SelfLink,
    TargetNotFound,
)
from sreditor.context_operations import CatalogQuery, ContextChange, LinkQuery
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.mcp_server import Effect, Removed, principal, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, Page

# How many links a single write may add, so one call stays small.
MAX_LINKS_PER_WRITE = 20


class ContextView(BaseModel):
    """An item, its answers, a page of the links it makes and a page of those made to it."""

    model_config = ConfigDict(frozen=True)

    item: LinkedItem
    context: ItemContext
    links: Page[Link]
    mentions: Page[Link]


class ContextUpdate(BaseModel):
    """New answers (all six, replacing the old ones) and links to add; either may be left out."""

    model_config = ConfigDict(frozen=True)

    answers: ContextInput | None = None
    links: list[LinkInput] = Field(default_factory=list, max_length=MAX_LINKS_PER_WRITE)
    expected_revision: int | None = None


class ContextPages(BaseModel):
    """Where to continue each list of links; empty for the first page."""

    model_config = ConfigDict(frozen=True)

    links_cursor: str | None = None
    mentions_cursor: str | None = None


def context_error_message(error: Exception) -> str | None:
    """How to fix a refused context call (agentic.mcp.errors); None for other errors."""
    if isinstance(error, TargetNotFound):
        return (
            f"links.to: no {error.item.kind} {error.item.id} in this channel; call "
            "list_linkable_items for the kinds and ids a link can point to"
        )
    if isinstance(error, ItemNotFound):
        return (
            f"item: no {error.item.kind} {error.item.id} in this channel; call "
            "list_linkable_items for the channel's items"
        )
    if isinstance(error, LinkNotFound):
        return f"Link {error} not found; call get_context for the item's link ids"
    if isinstance(error, SelfLink):
        return f"links.to: {error}"
    return None


def context_reported[T](action: Callable[[], T]) -> T:
    """Run a context tool's action; context refusals name their fix, others go to `reported`."""

    def guarded() -> T:
        try:
            return action()
        except (ItemNotFound, LinkNotFound, SelfLink) as refusal:
            raise ToolError(context_error_message(refusal) or str(refusal)) from refusal

    return reported(guarded)


def channel_access(services: Services, call: Context, slug: str) -> ChannelAccess:
    """The caller's access to the channel, from the token behind the MCP request."""
    who = principal(call)
    return access_for(services, slug, who.user, who.token)


def context_view(
    services: Services, access: ChannelAccess, item: ItemRef, pages: ContextPages
) -> ContextView:
    """The item with its answers and first (or next) pages of links and mentions."""

    def page(direction: LinkDirection, cursor: str | None) -> Page[Link]:
        query = LinkQuery(item=item, direction=direction, cursor=cursor, limit=DEFAULT_LIMIT)
        return contexts.list_links(services, access, query)

    return ContextView(
        item=services.contexts.find(access.channel.slug, item),
        context=contexts.get_context(services, access, item),
        links=page(LinkDirection.OUT, pages.links_cursor),
        mentions=page(LinkDirection.IN, pages.mentions_cursor),
    )


def apply_update(
    services: Services, access: ChannelAccess, item: ItemRef, update: ContextUpdate
) -> None:
    """Check every link's target first, then write the answers and add the missing links."""
    for wanted in update.links:
        try:
            services.contexts.find(access.channel.slug, wanted.to)
        except ItemNotFound as missing:
            raise TargetNotFound(missing.item) from missing
    if update.answers is not None:
        change = ContextChange(item=item, expected_revision=update.expected_revision)
        contexts.write_context(services, access, change, update.answers)
    for wanted in update.links:
        try:
            contexts.add_link(services, access, item, wanted)
        except LinkExists:
            continue  # already there: a retried call changes nothing more


def add_context_tools(server: MCPServer, services: Services) -> None:
    """Tools to find items, read and write their context, and link or unlink them."""
    add_context_reads(server, services)
    add_context_writes(server, services)


def add_context_reads(server: MCPServer, services: Services) -> None:
    """Tools that read the channel's items and an item's context."""

    @server.tool(annotations=tool_annotations("List linkable items", Effect.READ))
    def list_linkable_items(
        call: Context,
        slug: str,
        kind: ItemKind | None = None,
        cursor: str | None = None,
        limit: int = Field(DEFAULT_LIMIT, ge=1, le=200),
    ) -> Page[LinkedItem]:
        """List the channel's items that keep a context and can be linked, with their kind, id
        and name: episodes (id = number), pitches, pitch_source, source (an episode's), media
        (an episode's file), external_media, character, location, prop and style. Use it to
        find the `kind` and `id` that get_context and write_context take."""
        return context_reported(
            lambda: contexts.catalog(
                services,
                channel_access(services, call, slug),
                CatalogQuery(kind=kind, cursor=cursor, limit=limit),
            )
        )

    @server.tool(annotations=tool_annotations("Read an item's context", Effect.READ))
    def get_context(
        call: Context, slug: str, item: ItemRef, pages: ContextPages | None = None
    ) -> ContextView:
        """Read an item's answers to what, who, where, when, why and how, the links it makes
        (each answering one question with another item, such as who = a character) and its
        `mentions`: links other items make to it. Revision 0 means nobody wrote the answers.
        Pass a page's next_cursor in `pages` to see more links or mentions."""
        return context_reported(
            lambda: context_view(
                services, channel_access(services, call, slug), item, pages or ContextPages()
            )
        )


def add_context_writes(server: MCPServer, services: Services) -> None:
    """Tools that write an item's context and its links."""

    @server.tool(annotations=tool_annotations("Write an item's context", Effect.REPLACE))
    def write_context(
        call: Context, slug: str, item: ItemRef, update: ContextUpdate
    ) -> ContextView:
        """Set an item's answers to what, who, where, when, why and how, and link it to other
        items of the channel, in one call. `answers` replaces all six (leave one empty when it
        does not apply); `links` are added, and a link that already exists is kept as it is.
        Point `who` at characters and `where` at locations of the library when they exist; a
        person or place outside the library stays a name in the answer. Every link's target is
        checked before anything is written. Pass `revision` from get_context as
        `expected_revision` (0 when unwritten). Needs content:write."""

        def write() -> ContextView:
            access = channel_access(services, call, slug)
            apply_update(services, access, item, update)
            return context_view(services, access, item, ContextPages())

        return context_reported(write)

    @server.tool(annotations=tool_annotations("Remove a link", Effect.REMOVE))
    def unlink_items(call: Context, slug: str, link_id: UUID) -> Removed:
        """Remove a link between two items, by the id get_context lists; neither item changes.
        Needs content:write."""

        def remove() -> Removed:
            contexts.remove_link(services, channel_access(services, call, slug), link_id)
            return Removed(kind="item_link", id=link_id)

        return context_reported(remove)
