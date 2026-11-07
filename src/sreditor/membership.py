"""The member rules, shared by the REST API, the MCP tools and the pages (rule agentic.mcp).

Each function takes a ChannelAccess and checks it with `access.require`, which also narrows an
API token to its scopes. Writes go through the stmt gate (rule agentic.gate); sessions skip it.
Refusals are MembershipError subclasses whose message is an English msgid, so the API can keep
its `detail` and the pages can translate it.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

from sreditor.accounts import Accounts
from sreditor.deps import ChannelAccess, Services
from sreditor.i18n import N_
from sreditor.members import ELEVATED_ROLES, Invitation, Member, Permission, Role
from sreditor.operations import gated


class MemberOperation:
    """stmt operation names that an agent's member writes are gated on."""

    INVITE = "member.invite"
    ROLE = "member.role"
    REMOVE = "member.remove"
    REVOKE_INVITATION = "invitation.revoke"


class MembershipError(ValueError):
    """A member change the rules refuse; str() is the English msgid."""

    message = N_("This member change is not allowed")

    def __init__(self) -> None:
        super().__init__(self.message)


class LastOwner(MembershipError):
    """The change would leave the channel without an owner."""

    message = N_("A channel needs at least one owner")


class NotAMember(MembershipError):
    """The person is not a member of the channel."""

    message = N_("Not a member of this channel")


class InvitationNotPending(MembershipError):
    """The invitation was accepted, revoked or has expired."""

    message = N_("This invitation is no longer pending")


class MemberChanged(MembershipError):
    """The member's role is no longer the one the caller read (agentic.concurrency.etag)."""

    message = N_("This member changed since you read it; read it again and retry")

    def __init__(self, current: Role | None) -> None:
        super().__init__()
        self.current = current


class Invite(BaseModel):
    """Who to invite, and with which role."""

    model_config = ConfigDict(frozen=True)

    email: EmailStr
    role: Role


def check_change(access: ChannelAccess, current: Role | None, new: Role | None) -> None:
    """Editors manage contributors and viewers; only owners grant or take owner and editor roles."""
    access.require(Permission.MANAGE_MEMBERS)
    if current in ELEVATED_ROLES or new in ELEVATED_ROLES:
        access.require(Permission.MANAGE_OWNERS)


def grantable_roles(access: ChannelAccess) -> list[Role]:
    """The roles this person may give or take away, in the order Role lists them."""
    if not access.can(Permission.MANAGE_MEMBERS):
        return []
    may_elevate = access.can(Permission.MANAGE_OWNERS)
    return [role for role in Role if may_elevate or role not in ELEVATED_ROLES]


def can_change(access: ChannelAccess, member: Member) -> bool:
    """Whether this person may change the member's role or remove them."""
    return member.role in grantable_roles(access)


def member_role(app: Services, slug: str, user_id: UUID) -> Role:
    """The member's role, or NotAMember."""
    role = app.members.role(slug, user_id)
    if role is None:
        raise NotAMember
    return role


def current_role(app: Services, slug: str, user_id: UUID, expected: Role | None) -> Role:
    """The member's role, refused with MemberChanged when it is not the one the caller read."""
    role = member_role(app, slug, user_id)
    if expected is not None and role is not expected:
        raise MemberChanged(role)
    return role


def member(app: Services, access: ChannelAccess, user_id: UUID) -> Member:
    """One member of the channel, or NotAMember."""
    found = {item.user_id: item for item in members(app, access)}.get(user_id)
    if found is None:
        raise NotAMember
    return found


def keep_an_owner(app: Services, slug: str, current: Role, new: Role | None) -> None:
    """Refuse a change that takes the role away from the channel's last owner."""
    if current is Role.OWNER and new is not Role.OWNER and app.members.owner_count(slug) <= 1:
        raise LastOwner


def members(app: Services, access: ChannelAccess) -> list[Member]:
    """The channel's members, ordered by email."""
    access.require(Permission.READ)
    return app.members.list(access.channel.slug)


def invite(app: Services, accounts: Accounts, access: ChannelAccess, data: Invite) -> Invitation:
    """Invite someone by email; the email goes out in the accounts' language."""
    check_change(access, None, data.role)
    return gated(
        app,
        access,
        MemberOperation.INVITE,
        lambda: accounts.invite(access.channel, data.email, data.role, access.user),
    )


def change_role(
    app: Services, access: ChannelAccess, user_id: UUID, role: Role, expected: Role | None = None
) -> Role:
    """Change a member's role, keeping at least one owner.

    `expected` is the role the caller read; the write also checks the role it validated, so a
    change made in between is refused instead of overwritten.
    """
    slug = access.channel.slug
    current = current_role(app, slug, user_id, expected)
    check_change(access, current, role)
    keep_an_owner(app, slug, current, role)
    is_changed = gated(
        app,
        access,
        MemberOperation.ROLE,
        lambda: app.members.set_role(slug, user_id, role, current),
    )
    if not is_changed:
        raise MemberChanged(app.members.role(slug, user_id))
    return role


def remove_member(
    app: Services, access: ChannelAccess, user_id: UUID, expected: Role | None = None
) -> None:
    """Remove a member. Anyone may leave a channel, except its last owner."""
    slug = access.channel.slug
    current = current_role(app, slug, user_id, expected)
    if user_id != access.user.id:
        check_change(access, current, None)
    keep_an_owner(app, slug, current, None)
    is_removed = gated(
        app, access, MemberOperation.REMOVE, lambda: app.members.remove(slug, user_id, current)
    )
    if not is_removed:
        raise MemberChanged(app.members.role(slug, user_id))


def pending_invitations(app: Services, access: ChannelAccess) -> list[Invitation]:
    """Invitations not yet accepted, for the people who manage members."""
    access.require(Permission.MANAGE_MEMBERS)
    return app.members.pending_invitations(access.channel.slug)


def revoke_invitation(app: Services, access: ChannelAccess, invitation_id: UUID) -> None:
    """Withdraw a pending invitation; its link stops working."""
    slug = access.channel.slug
    pending = {item.id: item for item in pending_invitations(app, access)}
    invitation = pending.get(invitation_id)
    if invitation is None:
        raise InvitationNotPending
    check_change(access, invitation.role, None)
    revoked = gated(
        app,
        access,
        MemberOperation.REVOKE_INVITATION,
        lambda: app.members.revoke_invitation(slug, invitation_id),
    )
    if not revoked:
        raise InvitationNotPending
