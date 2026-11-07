"""MCP tools for an episode's claims: the figures and facts it states and the sources that back
them (rules agentic.mcp, project.claims).

A small set (agentic.mcp.tools.outcomes): list the claims, add one, change one, record that a
source was checked, and remove one. Every result repeats the episode's unchecked claims as a
warning, which never blocks anything.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict

from sreditor import claim_operations as claims
from sreditor.claim_operations import ClaimChange, ClaimQuery, SourceCheck
from sreditor.claims import (
    BackingNotFound,
    Claim,
    ClaimInput,
    ClaimNotFound,
    ClaimWarnings,
    SourceMark,
    SourceNotAvailable,
)
from sreditor.deps import ChannelAccess, Services
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_server import Effect, Removed, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit
from sreditor.pitch_tools import channel_access


class ClaimsView(BaseModel):
    """A page of an episode's claims, with all its unchecked claims."""

    model_config = ConfigDict(frozen=True)

    claims: Page[Claim]
    warnings: ClaimWarnings


class ClaimResult(BaseModel):
    """A claim after a change, with the episode's unchecked claims."""

    model_config = ConfigDict(frozen=True)

    claim: Claim
    warnings: ClaimWarnings


class ClaimRemoval(BaseModel):
    """What was removed, with the episode's unchecked claims."""

    model_config = ConfigDict(frozen=True)

    removed: Removed
    warnings: ClaimWarnings


def claim_error_message(error: Exception) -> str | None:
    """How to fix a refused claim call (agentic.mcp.errors); None for other errors."""
    if isinstance(error, ClaimNotFound):
        return f"Claim {error} not found; call list_claims for the episode's claim ids"
    if isinstance(error, BackingNotFound):
        return (
            f"Source {error} does not back this claim; call list_claims to see the source ids "
            "of the claim, or update_claim to add it"
        )
    if isinstance(error, SourceNotAvailable):
        return (
            f"Source {error} is neither a source of this episode nor of one of its pitches; "
            "call get_research for the ids of the episode's sources and, under `pitches`, of "
            "each pitch's sources. To use another pitch's source, call set_episode_pitches first"
        )
    return None


def claim_reported[T](action: Callable[[], T]) -> T:
    """Run a claim tool's action; claim refusals name their own fix, others go to `reported`."""

    def guarded() -> T:
        try:
            return action()
        except (ClaimNotFound, BackingNotFound, SourceNotAvailable) as refusal:
            raise ToolError(claim_error_message(refusal) or str(refusal)) from refusal

    return reported(guarded)


def claim_result(services: Services, channel: ChannelAccess, claim: Claim) -> ClaimResult:
    """The claim with its episode's unchecked claims."""
    unchecked = claims.unchecked_claims(services, channel, claim.number)
    return ClaimResult(claim=claim, warnings=unchecked)


def add_claim_tools(server: MCPServer, services: Services) -> None:
    """Tools to read an episode's claims and to write them."""
    add_claim_reads(server, services)
    add_claim_writes(server, services)
    add_claim_changes(server, services)


def add_claim_reads(server: MCPServer, services: Services) -> None:
    """The tool that lists an episode's claims."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("List claims", Effect.READ))
    def list_claims(
        context: Context,
        slug: str,
        number: int,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> ClaimsView:
        """List an episode's claims (each figure or fact its screenplay or short script states)
        with their backing sources, and `warnings`: the claims that still have no checked
        primary source. Use it before publishing and after writing. To add a claim use
        add_claim; to find source ids use get_research. Pass `claims.next_cursor` as `cursor`
        for more."""

        def read() -> ClaimsView:
            channel = access(context, slug)
            query = ClaimQuery(number=number, cursor=cursor, limit=limit)
            return ClaimsView(
                claims=claims.list_claims(services, channel, query),
                warnings=claims.unchecked_claims(services, channel, number),
            )

        return claim_reported(read)


def add_claim_writes(server: MCPServer, services: Services) -> None:
    """Tools to add a claim and to remove one."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("Add a claim", Effect.CREATE))
    def add_claim(
        context: Context,
        slug: str,
        number: int,
        claim: ClaimInput,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, ClaimResult]:
        """Record a figure or fact an episode states: its quote, where it appears (a scene
        heading, a short-script block number, or free text) and the backing sources, by the ids
        get_research lists for the episode's sources and its pitches' sources, each marked
        primary or not. Nothing starts checked: call check_claim_source once you opened the
        source. Needs content:write. Each call adds another claim unless you pass an
        `idempotency_key`, any unique string per intended claim: a retry with the same key and
        claim returns the claim the first call made."""
        call = IdempotentCall(
            operation=f"add_claim/{slug}/{number}", arguments=claim, key=idempotency_key
        )

        def add() -> CallToolResult:
            channel = access(context, slug)
            return run_once(
                services,
                context,
                call,
                lambda: claim_result(
                    services, channel, claims.add_claim(services, channel, number, claim)
                ),
                ClaimResult,
            )

        return claim_reported(add)

    @server.tool(annotations=tool_annotations("Remove a claim", Effect.REMOVE))
    def remove_claim(
        context: Context, slug: str, claim_id: UUID, expected_revision: int | None = None
    ) -> ClaimRemoval:
        """Remove a claim and its backing links, by the `id` list_claims shows; the sources
        themselves stay. Use it when the episode no longer states the claim. Needs
        content:write."""

        def remove() -> ClaimRemoval:
            channel = access(context, slug)
            number = claims.episode_of_claim(services, channel, claim_id)
            change = ClaimChange(
                number=number, claim_id=claim_id, expected_revision=expected_revision
            )
            claims.remove_claim(services, channel, change)
            return ClaimRemoval(
                removed=Removed(kind="claim", id=claim_id),
                warnings=claims.unchecked_claims(services, channel, number),
            )

        return claim_reported(remove)


def add_claim_changes(server: MCPServer, services: Services) -> None:
    """Tools to change a claim and to record that one of its sources was checked."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("Update a claim", Effect.REPLACE))
    def update_claim(
        context: Context,
        slug: str,
        claim_id: UUID,
        claim: ClaimInput,
        expected_revision: int | None = None,
    ) -> ClaimResult:
        """Replace a claim's quote, place and backing sources: send every source it should
        have, since a source left out stops backing it, while a source that stays keeps its
        check. Use it to add a source, mark one primary or fix the quote. Needs content:write.
        Pass the claim's `revision` from list_claims as `expected_revision`."""

        def update() -> ClaimResult:
            channel = access(context, slug)
            number = claims.episode_of_claim(services, channel, claim_id)
            change = ClaimChange(
                number=number, claim_id=claim_id, expected_revision=expected_revision
            )
            changed = claims.replace_claim(services, channel, change, claim)
            return claim_result(services, channel, changed)

        return claim_reported(update)

    @server.tool(annotations=tool_annotations("Check a claim's source", Effect.REPLACE))
    def check_claim_source(
        context: Context,
        slug: str,
        claim_id: UUID,
        mark: SourceMark,
        expected_revision: int | None = None,
    ) -> ClaimResult:
        """Record that you opened one backing source of a claim and it supports the quote
        (`mark.is_checked` true), or take the mark back (false); `mark.source_id` is the
        source's id on the claim. The check names the API token that made it and when. A claim
        stops warning once a primary source is checked. Only check a source you actually read.
        Needs content:write. Pass the claim's `revision` as `expected_revision`."""

        def record() -> ClaimResult:
            channel = access(context, slug)
            number = claims.episode_of_claim(services, channel, claim_id)
            change = ClaimChange(
                number=number, claim_id=claim_id, expected_revision=expected_revision
            )
            check = SourceCheck(change=change, source_id=mark.source_id, is_checked=mark.is_checked)
            checked = claims.check_claim_source(services, channel, check)
            return claim_result(services, channel, checked)

        return claim_reported(record)
