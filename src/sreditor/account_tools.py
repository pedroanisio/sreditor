"""MCP tools for a channel's platform accounts and for the caller's own identity (agentic.mcp).

Two task-sized tools (agentic.mcp.tools.outcomes): take a wrongly added account off a channel
(adding one is put_account), and tell an agent which user and token it is and where it may act.
"""

from __future__ import annotations

from http import HTTPStatus

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from sreditor import operations, whoami_operations
from sreditor.channels import Platform
from sreditor.deps import Services, access_for
from sreditor.mcp_principal import principal
from sreditor.mcp_server import ChannelView, Effect, reported, tool_annotations
from sreditor.pagination import DEFAULT_LIMIT, ToolLimit
from sreditor.problems import ProblemError
from sreditor.whoami_operations import WhoAmI


def add_account_tools(server: MCPServer, services: Services) -> None:
    """Tools to remove a platform account and to read the caller's own identity."""

    @server.tool(annotations=tool_annotations("Remove platform account", Effect.REMOVE))
    def remove_account(
        context: Context, slug: str, platform: Platform, expected_revision: int | None = None
    ) -> ChannelView:
        """Remove the channel's account on one platform, for example one added with a wrong
        handle; add the right one again with put_account. The channel, its brand kit and its
        episodes stay, but the account's own description and brand overrides are lost, and posts
        already recorded for that platform stay in their episodes' delivery packages. Needs
        owner or editor role and the channels:write scope; pass the `revision` from get_channel
        as `expected_revision` to refuse the change if the channel moved on."""

        def remove() -> ChannelView:
            who = principal(context)
            found = access_for(services, slug, who.user, who.token)
            try:
                done = operations.delete_account(services, found, platform, expected_revision)
            except ProblemError as refusal:
                if refusal.status == HTTPStatus.NOT_FOUND:
                    raise ToolError(
                        f"{refusal.detail}; get_channel lists the accounts the channel has"
                    ) from refusal
                raise
            return ChannelView(channel=done.channel, revision=done.revision)

        return reported(remove)

    @server.tool(annotations=tool_annotations("Who am I", Effect.READ))
    def whoami(
        context: Context, cursor: str | None = None, limit: ToolLimit = DEFAULT_LIMIT
    ) -> WhoAmI:
        """Tell which user and API token you are acting as: the user's id, display name and
        email, the token's id, name, scopes, the channels it is limited to (null for all of
        the user's) and when it expires, and a page of the channels you reach, each with your
        role there (owner, editor, contributor or viewer; null for an instance admin who is not
        a member). Use it to find your user id for change_role or remove_member, or to see why
        a call was refused for a missing scope or role. The token's secret is never shown. Needs
        the channels:read scope, as list_channels does. Pass `next_cursor` as `cursor` for more
        channels."""
        who = principal(context)
        return reported(
            lambda: whoami_operations.who_am_i(services, who.user, who.token, cursor, limit)
        )
