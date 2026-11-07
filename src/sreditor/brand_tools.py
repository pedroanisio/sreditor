"""MCP tools for a channel's identity: its display name and its brand colors and fonts.

Two task-sized tools (agentic.mcp.tools.outcomes, project.brand): rename the channel, and set
its palette and fonts. Reading the kit is get_brand. Files (logo, avatar, stings, font files)
are not sent through these tools: they are uploaded on the brand page.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from sreditor import operations
from sreditor.brand import BrandPatch
from sreditor.channels import ChannelRename, Color, FontInput, FontRole, Platform
from sreditor.deps import ChannelAccess, Services, access_for
from sreditor.mcp_server import (
    BrandView,
    ChannelView,
    Effect,
    principal,
    reported,
    tool_annotations,
)

# What pydantic puts before the message of a ValueError raised by one of our validators.
VALUE_ERROR_PREFIX = "Value error, "
NOTHING_TO_CHANGE = (
    "Pass colors, fonts or both; call get_brand to see the colors and fonts the kit has now"
)


def brand_patch(
    colors: list[Color] | None, fonts: dict[FontRole, FontInput | None] | None
) -> BrandPatch:
    """The patch the arguments describe, or a tool error naming what to fix."""
    if colors is None and fonts is None:
        raise ToolError(NOTHING_TO_CHANGE)
    try:
        return BrandPatch(colors=colors, fonts=fonts)
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
    # The tool's flat arguments are the schema an agent reads; grouping them in a model would
    # nest them, so this one signature keeps six parameters, context included.
    def update_brand(  # noqa: PLR0913
        context: Context,
        slug: str,
        platform: Platform | None = None,
        colors: list[Color] | None = None,
        fonts: dict[FontRole, FontInput | None] | None = None,
        expected_revision: int | None = None,
    ) -> BrandView:
        """Set the brand's colors and fonts, as data. `colors` is the WHOLE palette, replacing
        the old one: each is a name, a #RRGGBB hex and a role (primary, secondary, accent, text,
        background); each role is used once, only accent may repeat. `fonts` sets the roles it
        names (heading, body, caption) with a family and a weight from 100 to 900, and removes a
        role whose value is null; roles it leaves out stay. What you leave out stays as it is.
        With `platform`, only that account's overrides change (it needs that account first,
        put_account). Read the current kit and its `revision` with get_brand first; pass the
        revision as `expected_revision` to refuse the change if someone else changed the
        channel. Not for the logo, avatar, stings or font files: those are uploaded on the
        brand page (signed upload links are for agents). Needs owner or editor role and the
        channels:write scope."""

        def write() -> BrandView:
            patch = brand_patch(colors, fonts)
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
