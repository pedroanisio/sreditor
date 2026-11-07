"""MCP tools for a channel's identity: its display name and its brand colors and fonts.

Two task-sized tools (agentic.mcp.tools.outcomes, project.brand): rename the channel, and set
its palette and fonts. Reading the kit is get_brand. Files (logo, avatar, stings, font files)
are not sent through these tools: they are uploaded on the brand page.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sreditor import operations
from sreditor.brand import BrandPatch
from sreditor.channels import ChannelRename, Color, FontInput, FontRole, Platform
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.mcp_principal import principal
from sreditor.mcp_server import BrandView, ChannelView, Effect, reported, tool_annotations

# What pydantic puts before the message of a ValueError raised by one of our validators.
VALUE_ERROR_PREFIX = "Value error, "
NOTHING_TO_CHANGE = "Pass colors, fonts or both in `brand`; call get_brand to see the colors and fonts the kit has now"


class BrandChanges(BaseModel):
    """The colors and fonts update_brand sets; what is left out stays as it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    colors: list[Color] | None = Field(
        default=None,
        description="The WHOLE palette, replacing the old one: name, #RRGGBB and role each.",
    )
    fonts: dict[FontRole, FontInput | None] | None = Field(
        default=None,
        description="Fonts by role (heading, body, caption); null removes a role's font.",
    )


def brand_patch(changes: BrandChanges) -> BrandPatch:
    """The patch the arguments describe, or a tool error naming what to fix."""
    if changes.colors is None and changes.fonts is None:
        raise ToolError(NOTHING_TO_CHANGE)
    try:
        return BrandPatch(colors=changes.colors, fonts=changes.fonts)
    except ValidationError as invalid:
        reasons = "; ".join(
            str(error["msg"]).removeprefix(VALUE_ERROR_PREFIX) for error in invalid.errors()
        )
        raise ToolError(
            f"The palette was refused: {reasons}. Send the whole palette again, with each "
            "role (primary, secondary, text, background) on one color and unique names; "
            "get_brand shows the current one"
        ) from invalid


def add_brand_tools(server: MCPServer, services: Services) -> None:
    """Tools to rename a channel and to set its brand colors and fonts."""

    def access(context: Context, slug: str) -> ChannelAccess:
        who = principal(context)
        return access_for(services, slug, who.user, who.token)

    @server.tool(annotations=tool_annotations("Rename channel", Effect.REPLACE))
    def update_channel(
        context: Context, slug: str, name: str, expected_revision: int | None = None
    ) -> ChannelView:
        """Change a channel's display name, such as "Bater pra Cima" for the slug
        baterparacima. The slug, which addresses the channel everywhere, never changes. Use it
        only to rename; set colors and fonts with update_brand, accounts with put_account.
        Needs owner or editor role and the channels:write scope; pass the `revision` from
        get_channel as `expected_revision` to refuse the change if the channel moved on."""

        def write() -> ChannelView:
            rename = ChannelRename(name=name)
            done = operations.rename_channel(
                services, access(context, slug), rename, expected_revision
            )
            return ChannelView(channel=done.channel, revision=done.revision)

        return reported(write)

    @server.tool(annotations=tool_annotations("Update brand colors and fonts", Effect.REPLACE))
    def update_brand(
        context: Context,
        slug: str,
        brand: BrandChanges,
        platform: Platform | None = None,
        expected_revision: int | None = None,
    ) -> BrandView:
        """Set the brand's colors and fonts, as data, inside `brand`. Its `colors` is the WHOLE
        palette, replacing the old one: each is a name, a #RRGGBB hex and a role (primary, secondary, accent, text,
        background); each role is used once, only accent may repeat. Its `fonts` sets the roles it
        names (heading, body, caption) with a family and a weight from 100 to 900, and removes a
        role whose value is null; roles it leaves out stay. What you leave out stays as it is.
        With `platform`, only that account's overrides change (it needs that account first,
        put_account). Read the current kit and its `revision` with get_brand first; pass the
        revision as `expected_revision` to refuse the change if someone else changed the
        channel. Not for the logo, avatar, stings or font files: those are uploaded on the
        brand page (signed upload links are for agents). Needs owner or editor role and the
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
