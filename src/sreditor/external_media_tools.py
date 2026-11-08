"""MCP tools for third-party media: the channel's library and each episode's uses (agentic.mcp).

A few task-sized tools (agentic.mcp.tools.outcomes): find items, add or update one, use one in
an episode, drop a use, and read what the episode must credit with its rights warnings.
Uploading a copy is a file transfer, so it stays on the API (upload_external_media_copy).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict

from sreditor import external_media_operations as external
from sreditor.deps import ChannelAccess, Services
from sreditor.external_media import (
    EpisodeCredits,
    ExternalKind,
    ExternalMedia,
    ExternalMediaInput,
    ExternalMediaInUse,
    ExternalMediaNotFound,
    MediaUse,
    MediaUseInput,
    MediaUseNotFound,
    UseRefused,
)
from sreditor.external_media_operations import ItemChange, ItemQuery, UseChange, UseQuery
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_server import Effect, Removed, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit, page_after
from sreditor.pitch_tools import channel_access

# Episode numbers are cursor keys compared as text, so they are zero-padded to sort as numbers.
CURSOR_DIGITS = 10


class UseTarget(BaseModel):
    """One use of third-party media: the episode's number and the use's id from get_episode_credits."""

    model_config = ConfigDict(frozen=True)

    number: int
    use_id: UUID


class EpisodeExternalMedia(BaseModel):
    """An episode's credits and rights warnings, with a page of its uses."""

    model_config = ConfigDict(frozen=True)

    credits: EpisodeCredits
    uses: Page[MediaUse]


def external_media_error_message(error: Exception) -> str | None:
    """How to fix a refused third-party media call (agentic.mcp.errors); None for others."""
    if isinstance(error, ExternalMediaNotFound):
        return f"Third-party item {error} not found; call list_external_media for the item ids"
    if isinstance(error, MediaUseNotFound):
        return f"Use {error} not found; call get_episode_credits for the episode's use ids"
    if isinstance(error, ExternalMediaInUse):
        numbers = ", ".join(str(number) for number in error.episodes)
        return (
            f"Third-party item {error} is used by episodes {numbers}; call "
            "list_external_media_episodes to list them again, then get_episode_credits for "
            "each episode's use ids and remove_external_media_use for each use of this item first"
        )
    if isinstance(error, UseRefused):
        return f"{error} (field {error.field}); leave it out or pick an item of another kind"
    return None


def external_reported[T](action: Callable[[], T]) -> T:
    """Run a tool's action; third-party media refusals name their own fix."""

    def guarded() -> T:
        try:
            return action()
        except (
            ExternalMediaNotFound,
            MediaUseNotFound,
            ExternalMediaInUse,
            UseRefused,
        ) as refusal:
            raise ToolError(external_media_error_message(refusal) or str(refusal)) from refusal

    return reported(guarded)


def add_external_media_tools(server: MCPServer, services: Services) -> None:
    """Tools to keep the channel's third-party media and use it in episodes."""
    add_library_items(server, services)
    add_episode_uses(server, services)


def add_library_items(server: MCPServer, services: Services) -> None:
    """Tools to list, add and update the channel's third-party items."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("List third-party media", Effect.READ))
    def list_external_media(
        context: Context,
        slug: str,
        kind: ExternalKind | None = None,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[ExternalMedia]:
        """List a channel's third-party media: videos, images, audio, documents, web pages, 3D
        models, textures and shaders made by others, with their link, creator, rights and credit line, oldest first. Pass
        `kind` to narrow and `next_cursor` as `cursor` for more. Check here before adding."""
        query = ItemQuery(kind=kind, cursor=cursor, limit=limit)
        return external_reported(
            lambda: external.list_items(services, access(context, slug), query)
        )

    @server.tool(annotations=tool_annotations("Add third-party media", Effect.CREATE))
    def add_external_media(
        context: Context,
        slug: str,
        item: ExternalMediaInput,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, ExternalMedia]:
        """Add an item made by others to a channel's library, such as a NASA clip, a video
        the channel reacts to, a credited photo, a paper or a CC BY 3D model, texture or
        shader: its kind, title, the page where it
        lives, creator, rights and credit line. sreditor never downloads from the link. Each
        call adds another item unless you pass an `idempotency_key` (a retry with the same key and
        item returns the first one), so check list_external_media first. Needs content:write."""
        call = IdempotentCall(
            operation=f"add_external_media/{slug}", arguments=item, key=idempotency_key
        )
        return external_reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: external.create_item(services, access(context, slug), item),
                ExternalMedia,
            )
        )

    @server.tool(annotations=tool_annotations("List an item's episodes", Effect.READ))
    def list_external_media_episodes(
        context: Context,
        slug: str,
        media_id: UUID,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[int]:
        """List the numbers of the episodes that use a third-party item (from
        list_external_media), lowest first; the item cannot be deleted while any do. Use
        get_episode_credits on an episode to find the ids of its uses. Pass `next_cursor` as
        `cursor` for more."""

        def read() -> Page[int]:
            numbers = external.item_episodes(services, access(context, slug), media_id)
            return page_after(numbers, cursor, limit, lambda number: f"{number:0{CURSOR_DIGITS}d}")

        return external_reported(read)

    @server.tool(annotations=tool_annotations("Delete third-party media", Effect.REMOVE))
    def delete_external_media(
        context: Context, slug: str, media_id: UUID, expected_revision: int | None = None
    ) -> Removed:
        """Remove an item from the channel's library for good, with its uploaded copy. Refused
        while episodes use it: list_external_media_episodes lists them, get_episode_credits
        gives each episode's use ids and remove_external_media_use drops each use. Needs owner
        or editor role and the channels:write scope. Pass the item's `revision` from
        list_external_media as `expected_revision` to refuse the delete if the item changed."""
        change = ItemChange(media_id=media_id, expected_revision=expected_revision)

        def delete() -> Removed:
            external.delete_item(services, access(context, slug), change)
            return Removed(kind="external_media", id=media_id)

        return external_reported(delete)

    @server.tool(annotations=tool_annotations("Update third-party media", Effect.REPLACE))
    def update_external_media(
        context: Context,
        slug: str,
        media_id: UUID,
        item: ExternalMediaInput,
        expected_revision: int | None = None,
    ) -> ExternalMedia:
        """Replace an item's details, for example to record its license or credit line once
        known; its uploaded copy stays. Needs content:write. Pass the item's `revision` from
        list_external_media as `expected_revision` so you never overwrite unseen changes."""
        change = ItemChange(media_id=media_id, expected_revision=expected_revision)
        return external_reported(
            lambda: external.replace_item(services, access(context, slug), change, item)
        )


def add_episode_uses(server: MCPServer, services: Services) -> None:
    """Tools to use items in an episode, drop a use and read the episode's credits."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("Use third-party media", Effect.CREATE))
    def use_external_media(
        context: Context,
        slug: str,
        number: int,
        use: MediaUseInput,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, MediaUse]:
        """Record one use of a library item (from list_external_media) in an episode: a time
        range in seconds for video and audio, pages for documents and web pages, the purpose,
        the scene and an optional credit. Each call adds another use unless you pass an
        `idempotency_key`: a retry with the same key and use returns the first one. Needs
        content:write."""
        call = IdempotentCall(
            operation=f"use_external_media/{slug}/{number}", arguments=use, key=idempotency_key
        )
        return external_reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: external.add_use(services, access(context, slug), number, use),
                MediaUse,
            )
        )

    @server.tool(annotations=tool_annotations("Update a third-party media use", Effect.REPLACE))
    def update_external_media_use(
        context: Context,
        slug: str,
        target: UseTarget,
        use: MediaUseInput,
        expected_revision: int | None = None,
    ) -> MediaUse:
        """Replace one use of a library item in an episode, in place: its item, time range or
        pages, purpose, scene, credit and note. Use it to fix the warnings get_episode_credits
        lists for a use (a missing reason, range or credit) instead of removing the use and
        adding it again. `target` is the episode `number` and the `use_id` from
        get_episode_credits. Send the whole use, as use_external_media takes it; what you leave
        out is cleared. Needs content:write. Pass the use's `revision` as `expected_revision`
        so you never overwrite a change you have not read."""
        change = UseChange(
            number=target.number, use_id=target.use_id, expected_revision=expected_revision
        )
        return external_reported(
            lambda: external.replace_use(services, access(context, slug), change, use)
        )

    @server.tool(annotations=tool_annotations("Remove a third-party media use", Effect.REMOVE))
    def remove_external_media_use(
        context: Context,
        slug: str,
        number: int,
        use_id: UUID,
        expected_revision: int | None = None,
    ) -> Removed:
        """Take a use (by the id get_episode_credits lists) out of an episode; the item stays
        in the library. Needs content:write."""
        change = UseChange(number=number, use_id=use_id, expected_revision=expected_revision)

        def remove() -> Removed:
            external.remove_use(services, access(context, slug), change)
            return Removed(kind="external_media_use", id=use_id)

        return external_reported(remove)

    @server.tool(annotations=tool_annotations("Read an episode's credits", Effect.READ))
    def get_episode_credits(
        context: Context,
        slug: str,
        number: int,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> EpisodeExternalMedia:
        """Read the credit lines an episode owes for third-party media, its rights warnings
        (unknown rights, a citation with no reason or time range, a missing credit) and a page
        of its uses. Warnings never block a status; fix a use's warning with
        update_external_media_use, and an item's rights or credit line with
        update_external_media."""

        def read() -> EpisodeExternalMedia:
            channel = access(context, slug)
            query = UseQuery(number=number, cursor=cursor, limit=limit)
            return EpisodeExternalMedia(
                credits=external.get_credits(services, channel, number),
                uses=external.list_uses(services, channel, query),
            )

        return external_reported(read)
