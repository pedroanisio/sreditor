"""MCP server at /mcp: the main channel operations as tools for agents (rule agentic.mcp).

Tools call the operations layer, never HTTP routes or SQL. Callers authenticate with the same
personal API tokens as the REST API; a token's scopes and its user's roles apply unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from sreditor import (
    episode_operations,
    library_operations,
    media_operations,
    membership,
    operations,
    research_operations,
    screenplay_operations,
    waiver_operations,
)
from sreditor.accounts import AccountError
from sreditor.assets import AssetRejected
from sreditor.channels import BrandKit, Channel, Platform, PlatformAccount
from sreditor.deps import AGENT_LIMIT_DETAIL, ChannelAccess, Services, access_for, agent_retry_after
from sreditor.editorial import (
    Editorial,
    Guideline,
    GuidelineChanged,
    GuidelineInput,
    GuidelineNotFound,
)
from sreditor.episode_texts import PartChanged
from sreditor.episodes import (
    Episode,
    EpisodeChanged,
    EpisodeInput,
    EpisodeNotFound,
    RevisedEpisode,
)
from sreditor.i18n import BASE_LOCALE, match_locale
from sreditor.idempotency import Idempotency, request_hash
from sreditor.idempotency_store import IdempotencyStore
from sreditor.library import (
    EntryExists,
    EntryInput,
    EntryNotFound,
    EpisodeStyle,
    LibraryEntry,
    LibraryKind,
    StyleChoice,
    StyleNotInLibrary,
)
from sreditor.library_operations import Cast, EntryChange
from sreditor.media import MediaFile, MediaNotFound
from sreditor.media_operations import MediaChange, MediaQuery
from sreditor.members import Invitation, Member, Permission
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit, page_after
from sreditor.problems import ProblemError
from sreditor.repository import AccountTaken, ChannelExists, ChannelNotFound, RevisionMismatch
from sreditor.research import (
    NotesInput,
    ResearchNotes,
    Source,
    SourceInput,
    SourceNotFound,
)
from sreditor.research_operations import SourceChange
from sreditor.screenplay import Screenplay, ScreenplayInput
from sreditor.srstudio_operations import NotInLibrary
from sreditor.store import StoreError
from sreditor.tokens import ApiToken, is_api_token
from sreditor.users import User
from sreditor.waivers import Waiver, WaiverNotFound

PATH = "/mcp"
# RFC 9728 path-specific metadata for this resource, served by oauth_routes.
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource" + PATH
PRINCIPAL = "principal"
BEARER = "bearer"
LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
INSTRUCTIONS = (
    "sreditor manages social media channels: their accounts on each platform, brand kits and "
    "Editorial guidelines, and produces their episodes. Start with list_channels, read a channel "
    "with get_channel, and read its Editorial with get_editorial before writing content for it. "
    "A channel's episodes are listed with list_episodes and numbered from 1; read an episode's "
    "research with get_research before writing about it; an episode may also use one of the "
    "channel's pitches (story ideas with their own research, list_pitches and get_pitch), set "
    "with set_episode_pitch. Read its screenplay, in Fountain, with "
    "get_screenplay, which also shows which names the channel's library of characters and "
    "locations (list_library) still lacks. The library also holds visual styles; each episode "
    "picks one with set_episode_style. A vertical-video cut lives in the episode's short "
    "script (get_short_script, write_short_script); owners and editors can speak it with "
    "generate_voice_over, which spends ElevenLabs credits. An episode's voice-over, sound effects, score and "
    "visual effects are files listed with list_media; upload them with the API's upload_media. "
    "Media made by others (a NASA clip, a video the channel reacts to, a credited photo, a "
    "paper) lives in the channel's third-party library (list_external_media); record each use "
    "with use_external_media and check get_episode_credits for credits and rights warnings. "
    "Propose an episode's editorial classification with classify_episode (values from "
    "get_classification_vocabulary; a person confirms it) and compare the channel's declared "
    "and observed profile with get_channel_profile. "
    "Episodes, pitches, sources, media and library entries each keep a context (what, who, "
    "where, when, why, how) and links to one another: read it with get_context, set answers "
    "and links with write_context, and find ids with list_linkable_items. "
    "Owners and editors can list an episode's signed waivers with list_waivers."
)

# Domain errors an agent can act on, reported as tool errors with their message.
EXPECTED_ERRORS = (
    AccountError,
    ProblemError,
    HTTPException,
    ChannelNotFound,
    ChannelExists,
    GuidelineNotFound,
    GuidelineChanged,
    RevisionMismatch,
    EpisodeNotFound,
    EpisodeChanged,
    AccountTaken,
    SourceNotFound,
    PartChanged,
    EntryExists,
    EntryNotFound,
    MediaNotFound,
    WaiverNotFound,
    StyleNotInLibrary,
    AssetRejected,
    NotInLibrary,
    StoreError,
)


class Effect(StrEnum):
    """What a tool does to sreditor's data, which decides its annotations."""

    READ = "read"
    CREATE = "create"  # adds something each call, so repeating it adds another
    REPLACE = "replace"  # sets a value, so repeating it changes nothing more
    REMOVE = "remove"  # deletes or retires something people may still need


def tool_annotations(title: str, effect: Effect) -> ToolAnnotations:
    """All four hints, set explicitly (rule agentic.mcp.annotations).

    An unset hint means the worst case to a client: destructive, not idempotent and reaching
    outside the server. Every tool acts only inside sreditor, so none is open-world.
    """
    is_read = effect is Effect.READ
    return ToolAnnotations(
        title=title,
        readOnlyHint=is_read,
        destructiveHint=effect is Effect.REMOVE,
        idempotentHint=effect is not Effect.CREATE,
        openWorldHint=False,
    )


@dataclass(frozen=True)
class Principal:
    """The user and API token behind an MCP request."""

    user: User
    token: ApiToken


def error_message(error: Exception) -> str:
    """Turn an exception into the message shown to the agent, with a fix where there is one."""
    if isinstance(error, ProblemError):
        return error.detail
    if isinstance(error, HTTPException):
        return str(error.detail)
    if isinstance(error, ChannelNotFound):
        return f"Channel {error} not found; list_channels shows the channels you belong to"
    if isinstance(error, ChannelExists):
        return f"Channel {error} already exists; choose another slug"
    if isinstance(error, GuidelineNotFound):
        return f"Guideline {error} not found; get_editorial lists the ids of current guidelines"
    if isinstance(error, GuidelineChanged):
        return (
            f"Guideline {error} changed; it is now at revision {error.current}. Read it again "
            "with get_editorial, then retry with that revision as expected_revision"
        )
    if isinstance(error, RevisionMismatch):
        return (
            f"Channel {error} changed; it is now at revision {error.current}. Read it again "
            "with get_channel, then retry with that revision as expected_revision"
        )
    return production_error_message(error) or str(error)


def retry_hint(current: int) -> str:
    """How to retry after a stale expected_revision."""
    return (
        f"it is now at revision {current}. Read it again, merge your change and retry with "
        f"expected_revision={current}"
    )


# What was not found, and the call that lists the valid ids; {} is the missing id.
NOT_FOUND_HINTS: tuple[tuple[type[Exception], str], ...] = (
    (EpisodeNotFound, "Episode {} not found; call list_episodes for the channel's episode numbers"),
    (EntryNotFound, "Library entry {} not found; call list_library for the entry ids"),
    (SourceNotFound, "Source {} not found; call get_research for the episode's source ids"),
    (MediaNotFound, "Media {} not found; call list_media for the episode's media ids"),
    (WaiverNotFound, "Waiver {} not found; call list_waivers for the episode's waiver ids"),
    (StyleNotInLibrary, "{}; call list_library with kind style for the channel's style ids"),
)


def not_found_message(error: Exception) -> str | None:
    """How to find a valid id after a lookup failed."""
    for kind, hint in NOT_FOUND_HINTS:
        if isinstance(error, kind):
            return hint.format(error)
    return None


def production_error_message(error: Exception) -> str | None:
    """How to fix a refused episode, research, screenplay or library call (agentic.mcp.errors)."""
    if isinstance(error, EpisodeChanged | PartChanged):
        what = f"Episode {error}" if isinstance(error, EpisodeChanged) else str(error)
        return f"{what} changed; {retry_hint(error.current)}"
    if isinstance(error, EntryExists):
        return (
            f"{error}; call list_library to find its entry_id, then save_library_entry with "
            "that entry_id to change it"
        )
    if isinstance(error, AssetRejected | NotInLibrary):
        return str(error)
    return not_found_message(error)


def reported[T](action: Callable[[], T]) -> T:
    """Run a tool's action, turning refusals into tool errors the agent can read."""
    try:
        return action()
    except EXPECTED_ERRORS as refusal:
        raise ToolError(error_message(refusal)) from refusal


class BearerTokens:
    """ASGI middleware: only requests with a live API token reach the MCP transport."""

    def __init__(self, app: ASGIApp, services: Services) -> None:
        self.app = app
        self.services = services

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Let non-HTTP scopes pass; answer 401 or 429 before a request reaches the transport."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        principal = self.authenticate(scope)
        if principal is None:
            await self.unauthorized(send)
            return
        wait = agent_retry_after(self.services, principal.token)
        if wait is not None:
            await self.too_many(send, wait)
            return
        scope.setdefault("state", {})[PRINCIPAL] = principal
        await self.app(scope, receive, send)

    def authenticate(self, scope: Scope) -> Principal | None:
        """The principal behind the request's bearer API token, or None without a live token."""
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        kind, _, credential = headers.get("authorization", "").partition(" ")
        if kind.lower() != BEARER or not is_api_token(credential):
            return None
        found = self.services.tokens.authenticate(credential)
        return Principal(*found) if found else None

    async def unauthorized(self, send: Send) -> None:
        """401 that points MCP clients to OAuth discovery (rule agentic.oauth)."""
        metadata = self.services.settings.public_url.rstrip("/") + RESOURCE_METADATA_PATH
        challenge = f'Bearer resource_metadata="{metadata}"'
        await send_problem(
            send,
            HTTPStatus.UNAUTHORIZED,
            "Sign in with OAuth, or send a personal API token as `Authorization: Bearer sre_...`",
            [(b"www-authenticate", challenge.encode())],
        )

    async def too_many(self, send: Send, wait: int) -> None:
        """Answer 429 with the Retry-After header that says when a request is allowed again."""
        retry = (b"retry-after", str(wait).encode())  # agentic.rate-limits
        await send_problem(send, HTTPStatus.TOO_MANY_REQUESTS, AGENT_LIMIT_DETAIL, [retry])


async def send_problem(
    send: Send, code: HTTPStatus, detail: str, headers: list[tuple[bytes, bytes]]
) -> None:
    """Answer with an RFC 9457 problem before the request reaches the MCP transport."""
    body = json.dumps(
        {
            "type": "about:blank",
            "title": code.phrase,
            "status": code.value,
            "detail": detail,
            "instance": PATH,
        }
    ).encode()
    start_headers = [(b"content-type", b"application/problem+json"), *headers]
    await send({"type": "http.response.start", "status": code.value, "headers": start_headers})
    await send({"type": "http.response.body", "body": body})


def principal(context: Context) -> Principal:
    """The principal that BearerTokens authenticated for this tool call."""
    found: Principal = context.request_context.request.state.principal  # type: ignore[union-attr]
    return found


class ChannelView(BaseModel):
    """A channel with the revision to pass back as `expected_revision` when changing it."""

    model_config = ConfigDict(frozen=True)

    channel: Channel
    revision: int


def build(services: Services) -> MCPServer:
    """The MCP server with sreditor's tools, bound to the app's services."""
    # Imported here: pitch_tools builds on this module's helpers, so a top-level import
    # would be circular.
    from sreditor.classification_tools import add_classification_tools
    from sreditor.context_tools import add_context_tools
    from sreditor.external_media_tools import add_external_media_tools
    from sreditor.pitch_tools import add_pitch_tools
    from sreditor.short_script_tools import add_short_script_tools

    server: MCPServer = MCPServer(name="sreditor", instructions=INSTRUCTIONS)
    # Registration order is the order clients list the tools in.
    add_channel_tools(server, services)
    add_editorial_tools(server, services)
    add_episode_tools(server, services)
    add_research_tools(server, services)
    add_pitch_tools(server, services)
    add_screenplay_tools(server, services)
    add_short_script_tools(server, services)
    add_library_tools(server, services)
    add_style_tools(server, services)
    add_media_tools(server, services)
    add_external_media_tools(server, services)
    add_classification_tools(server, services)
    add_context_tools(server, services)
    add_waiver_tools(server, services)
    add_member_tools(server, services)
    return server


def add_channel_tools(server: MCPServer, services: Services) -> None:
    """Tools to list, read and create channels, set their accounts and read the brand kit."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("List channels", Effect.READ))
    def list_channels(
        context: Context, cursor: str | None = None, limit: ToolLimit = DEFAULT_LIMIT
    ) -> Page[Channel]:
        """List the channels you belong to, ordered by slug. Pass `next_cursor` for more."""
        who = principal(context)
        return reported(
            lambda: operations.list_channels(services, who.user, cursor, limit, who.token)
        )

    @server.tool(annotations=tool_annotations("Get channel", Effect.READ))
    def get_channel(context: Context, slug: str) -> ChannelView:
        """Read one channel: its platform accounts, descriptions and brand kit."""

        def read() -> ChannelView:
            found = access(context, slug)
            found.require(Permission.READ)
            return ChannelView(channel=found.channel, revision=found.revision)

        return reported(read)

    @server.tool(annotations=tool_annotations("Create channel", Effect.REPLACE))
    def create_channel(context: Context, channel: Channel) -> Channel:
        """Create a channel; you become its owner. Needs the channels:write scope and a token
        for all your channels. Set `language` to the language the channel publishes in, such as
        en-US: without it the channel is in pt-BR, and its scripts start in that language."""
        who = principal(context)
        return reported(lambda: operations.create_channel(services, who.user, who.token, channel))

    @server.tool(annotations=tool_annotations("Set platform account", Effect.REPLACE))
    def put_account(
        context: Context, slug: str, account: PlatformAccount, expected_revision: int | None = None
    ) -> ChannelView:
        """Add or replace the channel's account on one platform. Needs channels:write.

        Pass the `revision` from get_channel as `expected_revision` to refuse the change when
        someone else changed the channel in the meantime.
        """

        def write() -> ChannelView:
            done = operations.put_account(
                services, access(context, slug), account, expected_revision
            )
            return ChannelView(channel=done.channel, revision=done.revision)

        return reported(write)

    @server.tool(annotations=tool_annotations("Get brand kit", Effect.READ))
    def get_brand(context: Context, slug: str, platform: Platform | None = None) -> BrandKit:
        """The channel's brand kit; with a platform, that platform's overrides applied."""
        return reported(lambda: operations.get_brand(access(context, slug), platform))


def add_editorial_tools(server: MCPServer, services: Services) -> None:
    """Tools to read the Editorial and to write or retire its guidelines."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("Get Editorial", Effect.READ))
    def get_editorial(context: Context, slug: str, platform: Platform | None = None) -> Editorial:
        """The channel's Editorial: purpose, voice, audience, pillars and what to avoid.

        Read it before writing content for the channel; with a platform, its overrides apply.
        """
        return reported(lambda: operations.get_editorial(services, access(context, slug), platform))

    @server.tool(annotations=tool_annotations("Write guideline", Effect.CREATE))
    def write_guideline(
        context: Context,
        slug: str,
        guideline: GuidelineInput,
        expected_revision: int | None = None,
    ) -> Guideline:
        """Set the purpose, voice or audience, or add a pillar or avoid guideline.

        Needs the editorial:write scope, and the channel's rules must allow it. When setting
        purpose, voice or audience, pass its `revision` from get_editorial as
        `expected_revision` (0 when it is not set yet) so you never overwrite a text you have
        not read.
        """
        return reported(
            lambda: operations.write_guideline(
                services, access(context, slug), guideline, expected_revision
            )
        )

    @server.tool(annotations=tool_annotations("Retire guideline", Effect.REMOVE))
    def retire_guideline(
        context: Context,
        slug: str,
        statement_id: str,
        platform: Platform | None = None,
        expected_revision: int | None = None,
    ) -> str:
        """Retire one of the channel's guidelines by its id. Needs editorial:write.

        Pass the guideline's `revision` from get_editorial as `expected_revision` to refuse the
        change when someone revised it since you read it.
        """

        def retire() -> str:
            found = access(context, slug)
            operations.retire_guideline(services, found, statement_id, platform, expected_revision)
            return f"Retired {statement_id}"

        return reported(retire)


def add_member_tools(server: MCPServer, services: Services) -> None:
    """Tools to see who belongs to a channel and to invite someone to it."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("List members", Effect.READ))
    def list_members(
        context: Context, slug: str, cursor: str | None = None, limit: ToolLimit = DEFAULT_LIMIT
    ) -> Page[Member]:
        """The channel's members and their roles, by user id. Pass `next_cursor` for more."""

        def read() -> Page[Member]:
            members = membership.members(services, access(context, slug))
            return page_after(members, cursor, limit, lambda member: str(member.user_id))

        return reported(read)

    @server.tool(annotations=tool_annotations("Invite a member", Effect.CREATE))
    def invite_member(context: Context, slug: str, invite: membership.Invite) -> Invitation:
        """Email someone an invitation to join the channel with a role; they join when they
        accept it signed in. Needs members:write and a role that may grant that role (only
        owners invite owners). The email is written in the channel's language. Check
        list_members first: each call sends another email."""

        def send() -> Invitation:
            found = access(context, slug)
            locale = match_locale(found.channel.language) or BASE_LOCALE
            return membership.invite(services, services.accounts(locale), found, invite)

        return reported(send)


class Removed(BaseModel):
    """What a remove tool took away."""

    model_config = ConfigDict(frozen=True)

    kind: str
    id: UUID


class EntryEdit(EntryInput):
    """A character or location to add, or, with its entry_id, to change."""

    entry_id: UUID | None = Field(
        default=None, description="The entry to change, from list_library; omit to add one."
    )
    expected_revision: int | None = Field(
        default=None, description="The entry's revision as you read it, when changing it."
    )


class Research(BaseModel):
    """An episode's research notes and a page of the sources it cites."""

    model_config = ConfigDict(frozen=True)

    notes: ResearchNotes
    sources: Page[Source]


class ScreenplayWithCast(BaseModel):
    """An episode's screenplay and its names matched against the channel's library."""

    model_config = ConfigDict(frozen=True)

    screenplay: Screenplay
    cast: Cast


def add_episode_tools(server: MCPServer, services: Services) -> None:
    """Tools to list, read, create and change a channel's episodes."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("List episodes", Effect.READ))
    def list_episodes(
        context: Context, slug: str, cursor: str | None = None, limit: ToolLimit = DEFAULT_LIMIT
    ) -> Page[Episode]:
        """List a channel's episodes by number, to find the `number` other episode tools take.

        Pass `next_cursor` as `cursor` for the next page.
        """
        return reported(
            lambda: episode_operations.list_episodes(services, access(context, slug), cursor, limit)
        )

    @server.tool(annotations=tool_annotations("Read an episode", Effect.READ))
    def get_episode(context: Context, slug: str, number: int) -> RevisedEpisode:
        """Read one episode's title, summary and status, with the revision to pass as
        `expected_revision` to update_episode. For its research or screenplay, use get_research
        or get_screenplay instead."""
        return reported(
            lambda: episode_operations.get_episode(services, access(context, slug), number)
        )

    @server.tool(annotations=tool_annotations("Create an episode", Effect.CREATE))
    def create_episode(
        context: Context,
        slug: str,
        episode: EpisodeInput,
        idempotency_key: Annotated[str | None, Field(min_length=1, max_length=255)] = None,
    ) -> RevisedEpisode:
        """Add an episode with the channel's next number; it starts as an idea unless `status`
        says otherwise. Needs content:write. Each call creates another episode unless you pass
        an `idempotency_key`, any unique string per intended episode: a retry with the same key
        and episode returns the episode the first call made instead of a duplicate."""
        who = principal(context)
        fingerprint = request_hash(
            "MCP", f"create_episode/{slug}", episode.model_dump_json().encode()
        )
        once = Idempotency(
            IdempotencyStore(services.users.database), who.user.id, idempotency_key, fingerprint
        )
        return reported(
            lambda: once.once(
                lambda: episode_operations.create_episode(services, access(context, slug), episode),
                RevisedEpisode,
            )
        )

    @server.tool(annotations=tool_annotations("Update an episode", Effect.REPLACE))
    def update_episode(
        context: Context,
        slug: str,
        number: int,
        episode: EpisodeInput,
        expected_revision: int | None = None,
    ) -> RevisedEpisode:
        """Replace an episode's title, summary and status, for example to move it to script.
        Needs content:write. Call get_episode first and pass its `revision` as
        `expected_revision`, so a change someone else made in the meantime is never lost."""
        return reported(
            lambda: episode_operations.replace_episode(
                services, access(context, slug), number, episode, expected_revision
            )
        )


def add_research_tools(server: MCPServer, services: Services) -> None:
    """Tools to read and write an episode's research notes and sources."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(
        annotations=tool_annotations("Read an episode's research", Effect.READ),
    )
    def get_research(
        context: Context,
        slug: str,
        number: int,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Research:
        """Read an episode's research notes and the sources it cites, before writing about it.
        Notes at revision 0 were never written. Pass `sources.next_cursor` as `cursor` for more
        sources."""

        def read() -> Research:
            channel = access(context, slug)
            notes = research_operations.get_notes(services, channel, number)
            sources = research_operations.list_sources(services, channel, number, cursor, limit)
            return Research(notes=notes, sources=sources)

        return reported(read)

    @server.tool(
        annotations=tool_annotations("Write research notes", Effect.REPLACE),
    )
    def write_research_notes(
        context: Context,
        slug: str,
        number: int,
        notes: NotesInput,
        expected_revision: int | None = None,
    ) -> ResearchNotes:
        """Replace an episode's research notes with the whole new text; to cite where a fact
        comes from, use add_source instead. Needs content:write. Pass the notes' `revision` from
        get_research as `expected_revision` (0 when empty) so you never overwrite notes you have
        not read."""
        return reported(
            lambda: research_operations.write_notes(
                services, access(context, slug), number, notes, expected_revision
            )
        )

    @server.tool(annotations=tool_annotations("Cite a source", Effect.CREATE))
    def add_source(context: Context, slug: str, number: int, source: SourceInput) -> Source:
        """Cite a source for one claim of an episode: its link, title and the claim it supports.
        Needs content:write. `accessed_on` defaults to today. Each call adds another source."""
        return reported(
            lambda: research_operations.add_source(services, access(context, slug), number, source)
        )

    @server.tool(annotations=tool_annotations("Remove a source", Effect.REMOVE))
    def remove_source(
        context: Context,
        slug: str,
        number: int,
        source_id: UUID,
        expected_revision: int | None = None,
    ) -> Removed:
        """Stop citing a source, by the `id` get_research lists. Needs content:write."""
        change = SourceChange(
            number=number, source_id=source_id, expected_revision=expected_revision
        )

        def remove() -> Removed:
            research_operations.remove_source(services, access(context, slug), change)
            return Removed(kind="source", id=source_id)

        return reported(remove)


def add_screenplay_tools(server: MCPServer, services: Services) -> None:
    """Tools to read and write an episode's screenplay."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("Read a screenplay", Effect.READ))
    def get_screenplay(context: Context, slug: str, number: int) -> ScreenplayWithCast:
        """Read an episode's screenplay in Fountain with its scenes, places and speaking
        characters, each matched against the channel's library (entry null when the library has
        none; add it with save_library_entry). Revision 0 means nobody wrote it yet."""

        def read() -> ScreenplayWithCast:
            channel = access(context, slug)
            screenplay = screenplay_operations.get_screenplay(services, channel, number)
            cast = library_operations.episode_cast(services, channel, number)
            return ScreenplayWithCast(screenplay=screenplay, cast=cast)

        return reported(read)

    @server.tool(
        annotations=tool_annotations("Write a screenplay", Effect.REPLACE),
    )
    def write_screenplay(
        context: Context,
        slug: str,
        number: int,
        screenplay: ScreenplayInput,
        expected_revision: int | None = None,
    ) -> Screenplay:
        """Replace an episode's whole screenplay, written in Fountain. Needs content:write.

        Scene headings start with INT. or EXT.; a character's name in capitals, on its own line
        after a blank one, starts their dialogue. Pass the screenplay's `revision` from
        get_screenplay as `expected_revision` (0 when empty) so you never overwrite a version you
        have not read.
        """
        return reported(
            lambda: screenplay_operations.write_screenplay(
                services, access(context, slug), number, screenplay, expected_revision
            )
        )


def add_library_tools(server: MCPServer, services: Services) -> None:
    """Tools for the channel's library of characters and locations."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("List the library", Effect.READ))
    def list_library(
        context: Context,
        slug: str,
        kind: LibraryKind,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[LibraryEntry]:
        """List the channel's characters, locations or visual styles, with the ids
        save_library_entry, remove_library_entry and set_episode_style take. Pass `next_cursor`
        as `cursor` for more."""
        return reported(
            lambda: library_operations.list_entries(
                services, access(context, slug), kind, cursor, limit
            )
        )

    @server.tool(
        annotations=tool_annotations("Save a library entry", Effect.CREATE),
    )
    def save_library_entry(
        context: Context,
        slug: str,
        kind: LibraryKind,
        entry: EntryEdit,
    ) -> LibraryEntry:
        """Add a character, location or visual style (no `entry_id`), or rename or describe one
        (its `entry_id` from list_library, with its `revision` as `expected_revision`). Write a
        character's or location's name as the screenplay does; a style's description says how
        episodes look. Names are unique per kind ignoring case. Needs content:write."""
        data = EntryInput(name=entry.name, description=entry.description)

        def save() -> LibraryEntry:
            channel = access(context, slug)
            if entry.entry_id is None:
                return library_operations.create_entry(services, channel, kind, data)
            change = EntryChange(
                kind=kind, entry_id=entry.entry_id, expected_revision=entry.expected_revision
            )
            return library_operations.replace_entry(services, channel, change, data)

        return reported(save)

    @server.tool(
        annotations=tool_annotations("Remove a library entry", Effect.REMOVE),
    )
    def remove_library_entry(
        context: Context,
        slug: str,
        kind: LibraryKind,
        entry_id: UUID,
        expected_revision: int | None = None,
    ) -> Removed:
        """Remove a character, location or style by its id from list_library; screenplays that
        name it no longer match anything, and episodes that picked the style have none. Needs an
        owner or editor."""
        change = EntryChange(kind=kind, entry_id=entry_id, expected_revision=expected_revision)

        def remove() -> Removed:
            library_operations.delete_entry(services, access(context, slug), change)
            return Removed(kind=kind, id=entry_id)

        return reported(remove)


def add_style_tools(server: MCPServer, services: Services) -> None:
    """Tools to read and pick an episode's visual style from the library."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("Read an episode's visual style", Effect.READ))
    def get_episode_style(context: Context, slug: str, number: int) -> EpisodeStyle:
        """Read the visual style an episode picked from the channel's library, with its
        description; style is null when it picked none. Use it before describing visuals."""
        return reported(
            lambda: library_operations.get_episode_style(services, access(context, slug), number)
        )

    @server.tool(annotations=tool_annotations("Set an episode's visual style", Effect.REPLACE))
    def set_episode_style(
        context: Context,
        slug: str,
        number: int,
        style_id: UUID | None,
        expected_revision: int | None = None,
    ) -> EpisodeStyle:
        """Pick an episode's visual style by its id from list_library (kind style), or null for
        none. Add a missing style first with save_library_entry. Pass the `revision` from
        get_episode_style as `expected_revision` (0 before the first pick). Needs content:write."""
        choice = StyleChoice(style_id=style_id)
        return reported(
            lambda: library_operations.choose_episode_style(
                services, access(context, slug), number, choice, expected_revision
            )
        )


def add_media_tools(server: MCPServer, services: Services) -> None:
    """Tools for an episode's media files; uploads go through the API, not MCP."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("List an episode's media", Effect.READ))
    def list_media(
        context: Context,
        slug: str,
        number: int,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[MediaFile]:
        """List an episode's voice-over, sound effect, score and visual effect files, each with
        its kind, name, note, media type and size. The files themselves are uploaded and
        downloaded through the API (upload_media, download_media). Pass `next_cursor` as
        `cursor` for more."""
        query = MediaQuery(number=number, cursor=cursor, limit=limit)
        return reported(lambda: media_operations.list_media(services, access(context, slug), query))

    @server.tool(annotations=tool_annotations("Remove a media file", Effect.REMOVE))
    def remove_media(
        context: Context,
        slug: str,
        number: int,
        media_id: UUID,
        expected_revision: int | None = None,
    ) -> Removed:
        """Take a file out of an episode by its id from list_media, such as a discarded
        voice-over take. Pass its `revision` as `expected_revision` to be safe. Needs
        content:write."""
        change = MediaChange(number=number, media_id=media_id, expected_revision=expected_revision)

        def remove() -> Removed:
            media_operations.remove_media(services, access(context, slug), change)
            return Removed(kind="media", id=media_id)

        return reported(remove)


def add_waiver_tools(server: MCPServer, services: Services) -> None:
    """The tool to check an episode's waivers; uploads go through the API, not MCP."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("List an episode's waivers", Effect.READ))
    def list_waivers(
        context: Context,
        slug: str,
        number: int,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[Waiver]:
        """List the signed waivers (image, voice or location releases) behind an episode: who
        signed, when and what each covers. Use it to check that everyone who appears agreed.
        Waivers hold personal data: needs an owner or editor and the waivers:read scope. Files
        are uploaded and downloaded through the API (upload_waiver, download_waiver)."""
        return reported(
            lambda: waiver_operations.list_waivers(
                services, access(context, slug), number, cursor, limit
            )
        )


def transport_security(public_url: str) -> TransportSecuritySettings:
    """Accept the public host and local development hosts, refusing DNS-rebinding attempts."""
    public = urlsplit(public_url)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public.netloc, *LOCAL_HOSTS],
        allowed_origins=[f"{public.scheme}://{public.netloc}", *LOCAL_ORIGINS],
    )


def asgi_app(server: MCPServer, services: Services) -> ASGIApp:
    """The stateless, JSON-only transport behind the bearer-token check.

    Its session manager must run inside the FastAPI lifespan: `server.session_manager.run()`.
    """
    server.streamable_http_app(
        streamable_http_path=PATH,
        json_response=True,
        stateless_http=True,
        transport_security=transport_security(services.settings.public_url),
    )
    return BearerTokens(StreamableHTTPASGIApp(server.session_manager), services)
