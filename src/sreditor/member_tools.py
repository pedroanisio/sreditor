"""MCP tools to change a member's role or remove a member, which undo a wrong invitation.

Task-sized tools (agentic.mcp.tools.outcomes): list_members and invite_member are in
mcp_server; these two finish the job. They call the member rules in membership.py, the same
ones the REST API uses, so who may grant or take which role is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from sreditor import membership
from sreditor.deps import ChannelAccess, Services
from sreditor.field_types import ChannelSlug
from sreditor.mcp_server import Effect, Removed, reported, tool_annotations
from sreditor.members import Role
from sreditor.membership import LastOwner, MemberChanged, MembershipError, NotAMember
from sreditor.pitch_tools import channel_access


class MemberRole(BaseModel):
    """The role a member holds after change_role."""

    model_config = ConfigDict(frozen=True)

    channel: ChannelSlug
    user_id: UUID
    role: Role


def member_error_message(error: MembershipError, slug: str, user_id: UUID) -> str:
    """How to fix a refused member change (agentic.mcp.errors)."""
    if isinstance(error, NotAMember):
        return (
            f"User {user_id} is not a member of {slug}; call list_members for the user ids "
            "of its members"
        )
    if isinstance(error, LastOwner):
        return (
            f"{error}; call change_role to make another member an owner first, then retry "
            "(list_members shows the members)"
        )
    if isinstance(error, MemberChanged):
        now = error.current.value if error.current else "removed"
        return (
            f"The member changed since you read it (their role is now {now}); call "
            "list_members, then retry with that role as expected_role"
        )
    return str(error)


def member_reported[T](action: Callable[[], T], slug: str, user_id: UUID) -> T:
    """Run a member tool's action; member refusals name their own fix, others go to `reported`."""

    def guarded() -> T:
        try:
            return action()
        except MembershipError as refusal:
            raise ToolError(member_error_message(refusal, slug, user_id)) from refusal

    return reported(guarded)


def add_member_change_tools(server: MCPServer, services: Services) -> None:
    """Tools to change a member's role and to remove a member."""

    def access(context: Context, slug: str) -> ChannelAccess:
        return channel_access(services, context, slug)

    @server.tool(annotations=tool_annotations("Change a member's role", Effect.REPLACE))
    def change_role(
        context: Context,
        slug: str,
        user_id: UUID,
        role: Role,
        expected_role: Role | None = None,
    ) -> MemberRole:
        """Give a member another role (owner, editor, contributor or viewer), for example to
        fix a wrong invitation; find the `user_id` with list_members. Needs members:write and
        a role that may manage members: editors manage contributors and viewers, only owners
        grant or take the owner and editor roles. A channel always keeps at least one owner.
        Pass the member's current role from list_members as `expected_role` to refuse the
        change if someone else changed it meanwhile."""

        def change() -> MemberRole:
            membership.change_role(services, access(context, slug), user_id, role, expected_role)
            return MemberRole(channel=slug, user_id=user_id, role=role)

        return member_reported(change, slug, user_id)

    @server.tool(annotations=tool_annotations("Remove a member", Effect.REMOVE))
    def remove_member(
        context: Context, slug: str, user_id: UUID, expected_role: Role | None = None
    ) -> Removed:
        """Take a member off the channel, for example someone invited with a wrong email; find
        the `user_id` with list_members. Their work stays; they lose access, and inviting them
        again needs invite_member. Needs members:write and a role that may manage members
        (only owners remove owners and editors); the last owner cannot be removed. Removing
        your own user leaves the channel. Pass the member's current role from list_members as
        `expected_role` to refuse the change if someone else changed it meanwhile."""

        def remove() -> Removed:
            membership.remove_member(services, access(context, slug), user_id, expected_role)
            return Removed(kind="member", id=user_id)

        return member_reported(remove, slug, user_id)
