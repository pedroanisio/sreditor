"""MCP tools for a channel's identity: display name, status, brand colors, fonts and narrator voice.

Task-sized tools (agentic.mcp.tools.outcomes, project.brand): rename the channel or change its
language, move it between draft, active, paused and archived, and set its palette, fonts and
default narrator voice, and empty file slots. Reading the kit is get_brand. Files (logo, avatar,
stings, font files) are read with get_file_link and uploaded with create_upload_link, target
brand_file.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sreditor import operations
from sreditor.brand import BrandFileRef, BrandPatch
from sreditor.channels import (
    ChannelIdentity,
    Color,
    FontInput,
    FontRole,
    NarratorVoice,
    Platform,
    Status,
    Text,
)
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.field_types import LanguageTag
from sreditor.mcp_principal import principal
from sreditor.mcp_server import BrandView, ChannelView, Effect, reported, tool_annotations

# What pydantic puts before the message of a ValueError raised by one of our validators.
VALUE_ERROR_PREFIX = "Value error, "
NO_IDENTITY_CHANGE = (
    "Pass a new `name`, a new `language` or both; get_channel shows the current ones"
)
NOTHING_TO_CHANGE = (
    "Pass colors, fonts, narrator_voice, clear_files or several in `brand`; call get_brand to "
    "see what the kit has now"
)


class BrandChanges(BaseModel):
    """What update_brand sets or empties: palette, fonts, narrator voice and file slots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    colors: list[Color] | None = Field(
        default=None,
        description="The WHOLE palette, replacing the old one: name, #RRGGBB and role each.",
    )
    fonts: dict[FontRole, FontInput | None] | None = Field(
        default=None,
        description="Fonts by role (heading, body, caption); null removes a role's font.",
    )
    narrator_voice: NarratorVoice | None = Field(
        default=None,
        description=(
            "The voice that narrates a short script that names none: a voice_id from "
            "list_voices, an optional voice_name and tuning (stability, similarity_boost and "
            "style from 0 to 1, speed from 0.7 to 1.2, use_speaker_boost). null clears it."
        ),
    )
    clear_files: list[BrandFileRef] | None = Field(
        default=None,
        description=(
            "File slots to empty, each a `slot` (avatar, logo, banner, watermark, "
            "thumbnail_template, intro, outro or font) and, for a font, its `role`; the "
            "font's family stays. To put a file in a slot use create_upload_link."
        ),
    )


def brand_patch(changes: BrandChanges) -> BrandPatch:
    """The patch the arguments describe, or a tool error naming what to fix."""
    try:
        patch = BrandPatch.model_validate(changes.model_dump(exclude_unset=True))
    except ValidationError as invalid:
        reasons = "; ".join(
            str(error["msg"]).removeprefix(VALUE_ERROR_PREFIX) for error in invalid.errors()
        )
        raise ToolError(
            f"The palette was refused: {reasons}. Send the whole palette again, with each "
            "role (primary, secondary, text, background) on one color and unique names; "
            "get_brand shows the current one"
        ) from invalid
    if patch.is_empty:
        raise ToolError(NOTHING_TO_CHANGE)
    return patch


def add_brand_tools(server: MCPServer, services: Services) -> None:
    """Tools to rename a channel, change its language or status, and set its brand colors and fonts."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(
        annotations=tool_annotations("Rename channel or change its language", Effect.REPLACE)
    )
    def update_channel(
        context: Context,
        slug: str,
        name: Text | None = None,
        language: LanguageTag | None = None,
        expected_revision: int | None = None,
    ) -> ChannelView:
        """Change a channel's display name, such as "Bater pra Cima" for the slug
        baterparacima, its language as a BCP 47 tag such as pt-BR or en, or both; what you
        leave out stays. The language is what new short scripts speak and invitations are
        written in; scripts already written keep theirs. The slug, which addresses the channel
        everywhere, never changes. Set colors and fonts with update_brand, accounts with
        put_account. Needs owner or editor role and the channels:write scope; pass the
        `revision` from get_channel as `expected_revision` to refuse the change if the channel
        moved on."""

        def write() -> ChannelView:
            if name is None and language is None:
                raise ToolError(NO_IDENTITY_CHANGE)
            identity = ChannelIdentity(name=name, language=language)
            done = operations.change_identity(
                services, access(context, slug), identity, expected_revision
            )
            return ChannelView(channel=done.channel, revision=done.revision)

        return reported(write)

    @server.tool(annotations=tool_annotations("Set channel status", Effect.REPLACE))
    def set_channel_status(
        context: Context, slug: str, status: Status, expected_revision: int | None = None
    ) -> ChannelView:
        """Move a channel to draft, active, paused or archived, for example to archive one
        that is no longer published; its accounts, brand kit and episodes stay as they are.
        The name and language are changed with update_channel, an account's own status with
        put_account. Needs owner or editor role and the channels:write scope; pass the
        `revision` from get_channel as `expected_revision` to refuse the change if the channel
        moved on."""

        def write() -> ChannelView:
            done = operations.change_identity(
                services,
                access(context, slug),
                ChannelIdentity(status=status),
                expected_revision,
            )
            return ChannelView(channel=done.channel, revision=done.revision)

        return reported(write)

    @server.tool(
        annotations=tool_annotations("Update brand colors, fonts and files", Effect.REPLACE)
    )
    def update_brand(
        context: Context,
        slug: str,
        brand: BrandChanges,
        platform: Platform | None = None,
        expected_revision: int | None = None,
    ) -> BrandView:
        """Set the brand's colors, fonts, default narrator voice and empty file slots, inside
        `brand`.

        `colors` is the WHOLE palette, replacing the old one: each is a name, a #RRGGBB hex and
        a role (primary, secondary, accent, text, background); each role is used once, only
        accent may repeat. `fonts` sets the roles it
        names (heading, body, caption) with a family and a weight from 100 to 900, and removes a
        role whose value is null; roles it leaves out stay. `narrator_voice` is the voice a short
        script speaks with when it names none: a `voice_id` from list_voices with an optional
        `voice_name` and `tuning` (all of stability, similarity_boost and style from 0 to 1,
        speed from 0.7 to 1.2 and use_speaker_boost; leave `tuning` out to keep the voice's own
        settings); set it to null to clear it. Short scripts use the channel's narrator voice,
        so set it without `platform`. `clear_files` empties the file slots it names (each `slot`:
        avatar, logo, banner, watermark, thumbnail_template, intro, outro or font with its
        `role`; a font's family stays); on an account, the channel's file shows again. What you
        leave out stays as it is.
        With `platform`, only that account's overrides change (it needs that account first,
        put_account). Read the current kit and its `revision` with get_brand first; pass the
        revision as `expected_revision` to refuse the change if someone else changed the
        channel. This tool cannot upload a file: to put a logo, avatar, sting (intro is the
        opening, outro the closing) or font file in a slot, call create_upload_link with target
        brand_file; read one back with get_file_link. Needs owner or editor role and the
        channels:write scope."""

        def write() -> BrandView:
            patch = brand_patch(brand)
            done = operations.update_brand(
                services, access(context, slug), platform, patch, expected_revision
            )
            return BrandView(
                name=done.channel.name,
                revision=done.revision,
                platform=platform,
                brand=done.channel.brand_for(platform),
            )

        return reported(write)
