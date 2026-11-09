"""MCP server at /mcp: the main channel operations as tools for agents (rule agentic.mcp).

Tools call the operations layer, never HTTP routes or SQL. Callers authenticate with the same
personal API tokens as the REST API; a token's scopes and its user's roles apply unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import date
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
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from sreditor import (
    episode_operations,
    library_operations,
    media_operations,
    membership,
    operations,
    overview_operations,
    pitch_operations,
    research_operations,
    screenplay_operations,
    srstudio_operations,
    waiver_operations,
)
from sreditor.accounts import AccountError
from sreditor.assets import AssetRejected
from sreditor.channels import BrandKit, Channel, Platform, PlatformAccount
from sreditor.deps import (
    AGENT_LIMIT_DETAIL,
    ChannelAccess,
    RoleMissing,
    ScopeMissing,
    Services,
    access_for,
    agent_retry_after,
)
from sreditor.editorial import (
    CitedStatementNotFound,
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
from sreditor.experiments import EpisodeInExperiment
from sreditor.i18n import BASE_LOCALE, match_locale
from sreditor.library import (
    EntryExists,
    EntryInput,
    EntryNotFound,
    LibraryEntry,
    LibraryKind,
    StyleChoice,
    StyleNotInLibrary,
)
from sreditor.library_operations import Cast, EntryChange
from sreditor.mcp_aliases import RENAMED_TOOLS, AliasingServer, ToolRename
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_principal import PRINCIPAL, Principal, principal
from sreditor.media import MediaFile, MediaInput, MediaKind, MediaNotFound
from sreditor.media_operations import MediaChange, MediaQuery
from sreditor.members import Invitation, Member, Permission, minimum_role
from sreditor.pagination import DEFAULT_LIMIT, Page, ToolLimit, page_after
from sreditor.pitch_operations import PitchResearch
from sreditor.pitches import EpisodePitches
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
from sreditor.review_operations import ReadEpisode, read_episode
from sreditor.screenplay import Screenplay, ScreenplayInput
from sreditor.srstudio import EpisodeStyleDetail
from sreditor.srstudio_operations import NotInLibrary
from sreditor.store import StoreError
from sreditor.tokens import is_api_token
from sreditor.waivers import Waiver, WaiverNotFound

PATH = "/mcp"
# RFC 9728 path-specific metadata for this resource, served by oauth_routes.
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource" + PATH
BEARER = "bearer"
LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
INSTRUCTIONS = (
    "sreditor manages social media channels (platform accounts, brand kit, Editorial) and "
    "produces their episodes. Start with list_channels and get_channel; whoami says which user "
    "and token you act as and your role on each channel. create_channel, update_channel (name or "
    "language), set_channel_status (draft, active, paused, archived), put_account and "
    "remove_account change them; list_members, invite_member, change_role and remove_member "
    "manage who belongs. Read get_editorial before writing content for a channel, and change "
    "it with write_guideline and retire_guideline. get_brand and update_brand read and set "
    "its colors (with roles), fonts and default narrator voice. "
    "Episodes are numbered from 1: list_episodes (with readiness true it counts each "
    "episode's blockers and warnings), get_episode, create_episode and "
    "update_episode (status idea, research, script, production, published); "
    "get_episode_overview is the first call to read one episode: a digest of each part with "
    "its revision and the readiness list, each gap naming the tool that fixes it; "
    "delete_episode removes one for good after get_episode gives its revision. Research: "
    "get_research shows an episode's notes, sources and its pitches' research; "
    "write_research_notes, add_source, update_source and remove_source change it. Pitches are story ideas "
    "with their own research: list_pitches, get_pitch, create_pitch, update_pitch, "
    "write_pitch_notes, add_pitch_source and remove_pitch_source (update_source edits either "
    "kind of source in place); set_episode_pitches picks the ones an episode uses and "
    "delete_pitch removes one no episode uses. Record each figure or fact an episode states with list_claims, "
    "add_claim, update_claim, check_claim_source and remove_claim; claims without a checked "
    "primary source only warn. "
    "list_events lists what changed in a channel since your last cursor (writes by people, "
    "agents and render workers, render status changes), with the tool that reads each; "
    "it carries no content. "
    "primary source only warn. add_review records a review of an episode (verdict, notes, the "
    "file and time ranges seen); reviews are append-only and get_episode shows the latest, "
    "while an episode published without an approving review only warns. "
    "search_channel finds where a channel wrote about something (titles, research, sources, "
    "screenplays, short scripts, pitches, library, third-party media, context) and says which tool "
    "opens each hit. "
    "get_screenplay reads the Fountain screenplay and which names the channel's library still "
    "lacks; write_screenplay replaces it. The library of characters, locations and visual "
    "styles is list_library, save_library_entry and remove_library_entry; get_episode_style "
    "and set_episode_style pick a style, get_visual_style and save_visual_style read and set "
    "its palette, fonts, cut rhythm, smallest text and safe zone; save_prop adds a prop with "
    "the srstudio rig that draws it. What srstudio needs beyond that, which "
    "check_srstudio_export and request_render report as missing: get_library_sheet and "
    "save_library_sheet fill a character's, location's or prop's sheet (world, kind, rig); "
    "set_episode_slug sets the episode's slug and seed; list_relationships, save_relationship "
    "and remove_relationship keep how characters relate. Slugs are fixed once an srstudio "
    "export carried them. "
    "A vertical cut lives in the short script: get_short_script and write_short_script. "
    "Owners and editors speak it with generate_voice_over, which spends ElevenLabs credits: "
    "see list_voices and estimate_voice_over first, then read the timed words with "
    "get_voice_over and use them with apply_take_timings. "
    "Episode media files are listed with list_media, renamed with update_media and removed "
    "with remove_media. Files move "
    "through single-use links valid 15 minutes: get_file_link downloads a brand file, media "
    "file, render, third-party copy or srstudio export, and also an episode's whole data in one "
    "call (source export, scope episode, format json) or a channel summary or book (pdf, xlsx "
    "or json); create_upload_link uploads a media file; check_srstudio_export shows what an "
    "srstudio export lacks. list_waivers shows an episode's signed waivers to owners and "
    "editors; waivers are not sent through links. "
    "Media made by others lives in the third-party library: list_external_media, "
    "add_external_media and update_external_media; record each use with use_external_media, "
    "change it with update_external_media_use or drop it with remove_external_media_use and "
    "read get_episode_credits for credits and rights warnings; list_external_media_episodes "
    "shows which episodes use an item, and delete_external_media removes one that none uses. "
    "Propose a classification with classify_episode (values from "
    "get_classification_vocabulary; a person confirms it), read it with "
    "get_episode_classification and compare the channel's declared and observed profile with "
    "get_channel_profile, and set the declared one with declare_channel_profile. Episodes, "
    "pitches, sources, media and library entries each keep a "
    "context (what, who, where, when, why, how) and links: list_linkable_items finds ids, "
    "get_context reads and write_context sets them, unlink_items removes a link. "
    "Owners and editors render an episode with request_render, which answers at once; follow "
    "it with get_render (list_renders, cancel_render) until it is done, then fetch the video "
    "with get_file_link. "
    "An episode's delivery package (cuts and posts) is get_delivery and write_delivery; "
    "record_post adds a post's link, record_post_metrics and import_retention_csv enter its "
    "numbers, compare_posts ranks the channel's posts, what_worked compares them by "
    "classification, hook or length and list_post_snapshots shows one post's numbers day by "
    "day. "
    "An A/B experiment links two episodes with the change between them: save_experiment plans, "
    "starts or reads one and get_experiment lists them or shows one with its result computed "
    "from those numbers. "
    "Tools that create something or spend credits take an optional idempotency_key: retry with "
    "the same key and arguments after a timeout and you get the first result, marked replayed "
    "in the result's _meta, never a second copy. A call your token or role may not make says "
    "which scope to reconnect with or which role to ask a channel owner for."
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
    EpisodeInExperiment,
)


class Effect(StrEnum):
    """What a tool does to sreditor's data, which decides its annotations."""

    READ = "read"
    CREATE = "create"  # adds something each call, so repeating it adds another
    REPLACE = "replace"  # sets a value, so repeating it changes nothing more
    REMOVE = "remove"  # deletes or retires something people may still need


def tool_annotations(title: str, effect: Effect, *, is_open_world: bool = False) -> ToolAnnotations:
    """All four hints, set explicitly (rule agentic.mcp.annotations).

    An unset hint means the worst case to a client: destructive, not idempotent and reaching
    outside the server. Tools act inside sreditor, so only those that call ElevenLabs are
    open-world.
    """
    is_read = effect is Effect.READ
    return ToolAnnotations(
        title=title,
        readOnlyHint=is_read,
        destructiveHint=effect is Effect.REMOVE,
        idempotentHint=effect is not Effect.CREATE,
        openWorldHint=is_open_world,
    )


def permission_message(error: ScopeMissing | RoleMissing) -> str:
    """What a refused token or role lacks and how to get it (agentic.mcp.discovery).

    The two cases read alike to an agent but have different fixes: a scope comes from a new
    token or connection, a role from a channel owner.
    """
    if isinstance(error, ScopeMissing):
        return (
            f"This connection's token lacks the {error.scope} scope. Reconnect the MCP server, "
            f"or create a new API token, with the {error.scope} scope selected"
        )
    needed = minimum_role(error.permission)
    role = error.role.value if error.role else "none"
    return (
        f"Your role on channel {error.slug} is {role}, which cannot "
        f"{error.permission.value.replace('_', ' ')}; it needs the {needed.value} role. Ask a "
        f"channel owner to give you the {needed.value} role"
    )


def error_message(error: Exception) -> str:
    """Turn an exception into the message shown to the agent, with a fix where there is one."""
    if isinstance(error, ScopeMissing | RoleMissing):
        return permission_message(error)
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
    if isinstance(error, CitedStatementNotFound):
        return (
            f"Statement {error} does not exist or was retired, so a guideline cannot cite it. "
            "Check the id, or leave cited_statement_id out"
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
    if isinstance(error, EpisodeInExperiment):
        return (
            f"Episode {error} is compared by experiments "
            f"{', '.join(map(str, error.experiments))}; an experiment is deleted on its page or "
            "with the API's delete_experiment, after which the episode can go"
        )
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


class ChannelView(BaseModel):
    """A channel with the revision to pass back as `expected_revision` when changing it."""

    model_config = ConfigDict(frozen=True)

    channel: Channel
    revision: int


class BrandView(BaseModel):
    """A brand kit with the channel's name and the revision to pass as `expected_revision`."""

    model_config = ConfigDict(frozen=True)

    name: str
    revision: int
    platform: Platform | None = None
    brand: BrandKit


class MediaTarget(BaseModel):
    """One media file: the episode's number and the file's id from list_media."""

    model_config = ConfigDict(frozen=True)

    number: int
    media_id: UUID


def build(
    services: Services,
    renames: Sequence[ToolRename] = RENAMED_TOOLS,
    today: Callable[[], date] = date.today,
) -> AliasingServer:
    """The MCP server with sreditor's tools, bound to the app's services.

    `renames` and `today` are parameters so tests can check an alias before and after it ends.
    """
    # Imported here: pitch_tools builds on this module's helpers, so a top-level import
    # would be circular.
    from sreditor.account_tools import add_account_tools
    from sreditor.brand_tools import add_brand_tools
    from sreditor.claims_tools import add_claim_tools
    from sreditor.classification_tools import add_classification_tools
    from sreditor.context_tools import add_context_tools
    from sreditor.delivery_tools import add_delivery_tools
    from sreditor.event_tools import add_event_tools
    from sreditor.experiment_tools import add_experiment_tools
    from sreditor.external_media_tools import add_external_media_tools
    from sreditor.file_tools import add_file_tools
    from sreditor.member_tools import add_member_change_tools
    from sreditor.overview_tools import add_overview_tools
    from sreditor.pitch_tools import add_pitch_tools
    from sreditor.prop_tools import add_prop_tools
    from sreditor.render_tools import add_render_tools
    from sreditor.review_tools import add_review_tools
    from sreditor.search_tools import add_search_tools
    from sreditor.short_script_tools import add_short_script_tools
    from sreditor.source_tools import add_source_tools
    from sreditor.srstudio_tools import add_srstudio_tools
    from sreditor.style_tools import add_style_data_tools

    server = AliasingServer(name="sreditor", instructions=INSTRUCTIONS)
    # Registration order is the order clients list the tools in.
    add_channel_tools(server, services)
    add_account_tools(server, services)
    add_brand_tools(server, services)
    add_editorial_tools(server, services)
    add_episode_tools(server, services)
    add_overview_tools(server, services)
    add_event_tools(server, services)
    add_research_tools(server, services)
    add_pitch_tools(server, services)
    add_search_tools(server, services)
    add_source_tools(server, services)
    add_claim_tools(server, services)
    add_review_tools(server, services)
    add_screenplay_tools(server, services)
    add_short_script_tools(server, services)
    add_library_tools(server, services)
    add_style_tools(server, services)
    add_style_data_tools(server, services)
    add_prop_tools(server, services)
    add_srstudio_tools(server, services)
    add_media_tools(server, services)
    add_render_tools(server, services)
    add_delivery_tools(server, services)
    add_experiment_tools(server, services)
    add_file_tools(server, services)
    add_external_media_tools(server, services)
    add_classification_tools(server, services)
    add_context_tools(server, services)
    add_waiver_tools(server, services)
    add_member_tools(server, services)
    add_member_change_tools(server, services)
    server.add_aliases(renames, today())
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
    def create_channel(
        context: Context, channel: Channel, idempotency_key: IdempotencyKey = None
    ) -> Annotated[CallToolResult, Channel]:
        """Create a channel; you become its owner. Needs the channels:write scope and a token
        for all your channels. Set `language` to the language the channel publishes in, such as
        en-US: without it the channel is in pt-BR, and its scripts start in that language. Pass
        an `idempotency_key` to retry safely: a retry that repeats the call returns the channel
        the first call made, marked replayed, instead of "already exists"."""
        who = principal(context)
        call = IdempotentCall(operation="create_channel", arguments=channel, key=idempotency_key)
        return reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: operations.create_channel(services, who.user, who.token, channel),
                Channel,
            )
        )

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
    def get_brand(context: Context, slug: str, platform: Platform | None = None) -> BrandView:
        """The channel's brand kit: colors with roles, fonts by role, logo, avatar and opening
        and closing stings. With a platform, that platform's overrides are applied. Its
        `revision` is the expected_revision for update_brand and update_channel."""

        def read() -> BrandView:
            found = access(context, slug)
            kit = operations.get_brand(found, platform)
            return BrandView(
                name=found.channel.name, revision=found.revision, platform=platform, brand=kit
            )

        return reported(read)


def add_editorial_tools(server: MCPServer, services: Services) -> None:
    """Tools to read the Editorial and to write or retire its guidelines."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("Get Editorial", Effect.READ))
    def get_editorial(context: Context, slug: str, platform: Platform | None = None) -> Editorial:
        """The channel's Editorial: purpose, voice, audience, pillars and what to avoid.

        Read it before writing content for the channel; with a platform, its overrides apply.
        `warnings` lists each guideline that cites a stmt statement whose text now differs from
        its copy (statement id, both revisions, both texts) or that could not be checked. A
        person decides: to adopt the statement's text, call write_guideline with that text and
        the same cited_statement_id; to keep the copy, leave it. Do not use it to read stmt.
        """
        return reported(
            lambda: operations.read_editorial_checked(services, access(context, slug), platform)
        )

    @server.tool(annotations=tool_annotations("Write guideline", Effect.CREATE))
    def write_guideline(
        context: Context,
        slug: str,
        guideline: GuidelineInput,
        expected_revision: int | None = None,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, Guideline]:
        """Set the purpose, voice or audience, or add a pillar or avoid guideline. The text
        may use Markdown (headings, emphasis, lists, quotes, links), which pages and PDF books
        show formatted.

        Set `cited_statement_id` to the id of a stmt statement the guideline copies; get_editorial
        then warns when that statement's text differs from the copy. The id must name an enabled
        statement. Needs the editorial:write scope, and the channel's rules must allow it. When setting
        purpose, voice or audience, pass its `revision` from get_editorial as
        `expected_revision` (0 when it is not set yet) so you never overwrite a text you have
        not read. Each pillar or avoid call adds another guideline unless you pass an
        `idempotency_key`: a retry with the same key and guideline returns the first one.
        """
        call = IdempotentCall(
            operation=f"write_guideline/{slug}", arguments=guideline, key=idempotency_key
        )
        return reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: operations.write_guideline(
                    services, access(context, slug), guideline, expected_revision
                ),
                Guideline,
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
    def invite_member(
        context: Context,
        slug: str,
        invite: membership.Invite,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, Invitation]:
        """Email someone an invitation to join the channel with a role; they join when they
        accept it signed in. Needs members:write and a role that may grant that role (only
        owners invite owners). The email is written in the channel's language. Check
        list_members first: each call sends another email unless you pass an
        `idempotency_key`, which makes a retry return the first invitation without a new email."""

        def send() -> Invitation:
            found = access(context, slug)
            locale = match_locale(found.channel.language) or BASE_LOCALE
            return membership.invite(services, services.accounts(locale), found, invite)

        call = IdempotentCall(
            operation=f"invite_member/{slug}", arguments=invite, key=idempotency_key
        )
        return reported(lambda: run_once(services, context, call, send, Invitation))


class Removed(BaseModel):
    """What a remove tool took away."""

    model_config = ConfigDict(frozen=True)

    kind: str
    id: UUID


class EpisodeListing(Page[Episode]):
    """A page of episodes; `readiness` is filled, one entry per item, when asked for."""

    readiness: list[overview_operations.EpisodeReadiness] | None = None


class DeletedEpisode(BaseModel):
    """The episode delete_episode took away; its number is never given to another one."""

    model_config = ConfigDict(frozen=True)

    slug: str
    number: int


class EntryEdit(EntryInput):
    """A library entry to add, or, with its entry_id, to change."""

    entry_id: UUID | None = Field(
        default=None, description="The entry to change, from list_library; omit to add one."
    )
    expected_revision: int | None = Field(
        default=None, description="The entry's revision as you read it, when changing it."
    )


class Research(BaseModel):
    """An episode's research notes, a page of its sources and each of its pitches' research."""

    model_config = ConfigDict(frozen=True)

    notes: ResearchNotes
    sources: Page[Source]
    pitch_list: EpisodePitches
    pitches: list[PitchResearch] = Field(default_factory=list)


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
        context: Context,
        slug: str,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
        readiness: bool = False,
    ) -> EpisodeListing:
        """List a channel's episodes by number, to find the `number` other episode tools take.

        Pass `next_cursor` as `cursor` for the next page. With `readiness` true, `readiness`
        holds, for each episode of the page, its blockers and warnings (the counts of
        get_episode_overview's readiness list), to find which episode needs work; it is slower,
        so leave it off to just list.
        """

        def read() -> EpisodeListing:
            channel = access(context, slug)
            found = episode_operations.list_episodes(services, channel, cursor, limit)
            counts = (
                overview_operations.summaries_for(services, channel, found.items)
                if readiness
                else None
            )
            return EpisodeListing(
                items=found.items, next_cursor=found.next_cursor, readiness=counts
            )

        return reported(read)

    @server.tool(annotations=tool_annotations("Read an episode", Effect.READ))
    def get_episode(context: Context, slug: str, number: int) -> ReadEpisode:
        """Read one episode's title, summary and status, with the revision to pass as
        `expected_revision` to update_episode, and `reviews`: how many reviews it has and the
        latest one, whose verdict is the episode's (add_review records one). For its research
        or screenplay, use get_research or get_screenplay instead."""
        return reported(lambda: read_episode(services, access(context, slug), number))

    @server.tool(annotations=tool_annotations("Create an episode", Effect.CREATE))
    def create_episode(
        context: Context,
        slug: str,
        episode: EpisodeInput,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, RevisedEpisode]:
        """Add an episode with the channel's next number; it starts as an idea unless `status`
        says otherwise. The summary may use Markdown. Needs content:write. Each call creates
        another episode unless you pass
        an `idempotency_key`, any unique string per intended episode: a retry with the same key
        and episode returns the episode the first call made, marked replayed, instead of a
        duplicate."""
        call = IdempotentCall(
            operation=f"create_episode/{slug}", arguments=episode, key=idempotency_key
        )
        return reported(
            lambda: run_once(
                services,
                context,
                call,
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
        The summary may use Markdown. Needs content:write. Call get_episode first and pass its `revision` as
        `expected_revision`, so a change someone else made in the meantime is never lost."""
        return reported(
            lambda: episode_operations.replace_episode(
                services, access(context, slug), number, episode, expected_revision
            )
        )

    @server.tool(annotations=tool_annotations("Delete an episode", Effect.REMOVE))
    def delete_episode(
        context: Context, slug: str, number: int, expected_revision: int
    ) -> DeletedEpisode:
        """Delete an episode and every part of it (research, screenplay, short script, media,
        context); this cannot be undone and its number is never reused. Use it only when a
        person asked for this episode to go; to stop work on it, update_episode is enough.
        Call get_episode first and pass its `revision` as `expected_revision`, so an episode
        someone changed in the meantime is never deleted unseen. Needs channels:write and the
        owner or editor role. A whole channel can only be deleted on its pages, never here."""

        def delete() -> DeletedEpisode:
            channel = access(context, slug)
            episode_operations.delete_episode(services, channel, number, expected_revision)
            return DeletedEpisode(slug=slug, number=number)

        return reported(delete)


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
        """Read an episode's research notes and the sources it cites, before writing about it,
        and the notes and sources of each pitch it is built on (`pitches`, main one first, as
        they are now). `pitch_list` holds the pitch ids in order and the list's `revision`,
        which set_episode_pitches and set_episode_pitch take as `expected_revision`. Notes at
        revision 0 were never written. Pass `sources.next_cursor` as `cursor` for more
        sources."""

        def read() -> Research:
            channel = access(context, slug)
            notes = research_operations.get_notes(services, channel, number)
            sources = research_operations.list_sources(services, channel, number, cursor, limit)
            listed = pitch_operations.get_episode_pitches(services, channel, number)
            shown = pitch_operations.research_of_pitches(services, channel, listed)
            return Research(notes=notes, sources=sources, pitch_list=listed, pitches=shown)

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
        comes from, use add_source instead. Write them in Markdown (headings, emphasis, lists,
        quotes, links), which pages and PDF books show formatted. Needs content:write. Pass the
        notes' `revision` from
        get_research as `expected_revision` (0 when empty) so you never overwrite notes you have
        not read."""
        return reported(
            lambda: research_operations.write_notes(
                services, access(context, slug), number, notes, expected_revision
            )
        )

    @server.tool(annotations=tool_annotations("Cite a source", Effect.CREATE))
    def add_source(
        context: Context,
        slug: str,
        number: int,
        source: SourceInput,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, Source]:
        """Cite a source for one claim of an episode: its link, title and the claim it supports.
        Needs content:write. `accessed_on` defaults to today. Each call adds another source
        unless you pass an `idempotency_key`: a retry with the same key and source returns the
        first one."""
        call = IdempotentCall(
            operation=f"add_source/{slug}/{number}", arguments=source, key=idempotency_key
        )
        return reported(
            lambda: run_once(
                services,
                context,
                call,
                lambda: research_operations.add_source(
                    services, access(context, slug), number, source
                ),
                Source,
            )
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
    """Tools for the channel's library of characters, locations, styles and props."""

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
        """List the channel's characters, locations, visual styles or props, with the ids
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
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, LibraryEntry]:
        """Add a character, location, visual style or prop (no `entry_id`), or rename or describe one
        (its `entry_id` from list_library, with its `revision` as `expected_revision`). Write a
        character's or location's name as the screenplay does; a style's description says how
        episodes look. For a prop, prefer save_prop, which also sets the rig that draws it.
        Names are unique per kind ignoring case. Needs content:write. Pass an
        `idempotency_key` when adding, so a retry returns the first entry instead of "exists"."""
        data = EntryInput(name=entry.name, description=entry.description)

        def save() -> LibraryEntry:
            channel = access(context, slug)
            if entry.entry_id is None:
                return library_operations.create_entry(services, channel, kind, data)
            change = EntryChange(
                kind=kind, entry_id=entry.entry_id, expected_revision=entry.expected_revision
            )
            return library_operations.replace_entry(services, channel, change, data)

        call = IdempotentCall(
            operation=f"save_library_entry/{slug}/{kind}", arguments=entry, key=idempotency_key
        )
        return reported(lambda: run_once(services, context, call, save, LibraryEntry))

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
        """Remove a character, location, style or prop by its id from list_library; screenplays that
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
    def get_episode_style(context: Context, slug: str, number: int) -> EpisodeStyleDetail:
        """Read the visual style an episode picked from the channel's library: its description
        (style) and its structured sheet (srstudio style, palette, fonts, cut rhythm, smallest
        text, safe zone); both are null when it picked none. Use it before describing visuals."""
        return reported(
            lambda: srstudio_operations.get_episode_style_detail(
                services, access(context, slug), number
            )
        )

    @server.tool(annotations=tool_annotations("Set an episode's visual style", Effect.REPLACE))
    def set_episode_style(
        context: Context,
        slug: str,
        number: int,
        style_id: UUID | None,
        expected_revision: int | None = None,
    ) -> EpisodeStyleDetail:
        """Pick an episode's visual style by its id from list_library (kind style), or null for
        none. Add a missing style first with save_library_entry. Pass the `revision` from
        get_episode_style as `expected_revision` (0 before the first pick). Needs content:write."""
        choice = StyleChoice(style_id=style_id)
        return reported(
            lambda: srstudio_operations.choose_episode_style_detail(
                services, access(context, slug), number, choice, expected_revision
            )
        )


def add_media_tools(server: MCPServer, services: Services) -> None:
    """Tools for an episode's media files; the files themselves move through signed links."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    # An episode list with a filter needs six arguments: cursor and limit stay top-level like the
    # API's (agentic.mcp.results), and context is how the SDK hands over the caller.
    @server.tool(annotations=tool_annotations("List an episode's media", Effect.READ))
    def list_media(  # noqa: PLR0913
        context: Context,
        slug: str,
        number: int,
        kind: MediaKind | None = None,
        cursor: str | None = None,
        limit: ToolLimit = DEFAULT_LIMIT,
    ) -> Page[MediaFile]:
        """List an episode's media files (voice-over, sound effect, score, visual effect,
        cover, still, render and final video), each with its kind, name, note, media type, size
        and `sha256` (the SHA-256 of its content). Pass `kind` to list only one kind, such as
        cover or final_video for write_delivery, or still for frames kept as evidence. To
        get a file's bytes call get_file_link with source episode_media and the `id` listed
        here; to add a file call create_upload_link. Pass `next_cursor` as `cursor` for more."""
        query = MediaQuery(number=number, kind=kind, cursor=cursor, limit=limit)
        return reported(lambda: media_operations.list_media(services, access(context, slug), query))

    @server.tool(annotations=tool_annotations("Rename a media file", Effect.REPLACE))
    def update_media(
        context: Context,
        slug: str,
        target: MediaTarget,
        media: MediaInput,
        expected_revision: int | None = None,
    ) -> MediaFile:
        """Rename an episode's media file or change its note, in place; the file itself never
        changes, so a new take is a new file. `target` is the episode `number` and the
        `media_id` from list_media. Send the `name` and the `note` you want: a note left out is
        cleared. Needs content:write. Pass the file's `revision` from list_media as
        `expected_revision` so you never overwrite a change you have not read."""
        change = MediaChange(
            number=target.number, media_id=target.media_id, expected_revision=expected_revision
        )
        return reported(
            lambda: media_operations.rename_media(services, access(context, slug), change, media)
        )

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
        Waivers hold personal data: needs an owner or editor and the waivers:read scope. Their
        files never travel through MCP or its links (rule agentic.mcp.files): a person uploads
        or opens them on the episode's waivers page, or a script with an API token calls the
        API's upload_waiver and download_waiver operations."""
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
