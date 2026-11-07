"""The channel's members and invitations: invite, change roles, remove, revoke and leave."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.deps import ChannelAccess, Services, ServicesDep, within_rate_limit
from sreditor.i18n import N_, Translator
from sreditor.members import Member, Permission, Role
from sreditor.membership import (
    Invite,
    MembershipError,
    can_change,
    change_role,
    grantable_roles,
    invite,
    members,
    pending_invitations,
    remove_member,
    revoke_invitation,
)
from sreditor.problems import ProblemError
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.routes import TOO_MANY, TOO_MANY_ATTEMPTS, rejected_form, translator
from sreditor.web.workspace.forms import InviteFields, RoleFields
from sreditor.web.workspace.shell import (
    APP_PATH,
    Flash,
    Outcome,
    PageAccess,
    PageForbidden,
    PageNotFound,
    View,
    channel_page,
    channel_url,
    done,
    refused,
    require,
)

router = APIRouter()

MEMBERS_SUFFIX = "/members"
CHOOSE_ROLE = N_("Choose a role.")
ALREADY_MEMBER = N_("This person is already a member.")


class MembersView(BaseModel):
    """The errors to show, and the member row or form they belong to."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome = Outcome()
    target: str = ""
    sent: InviteFields | None = None


def member_rows(access: ChannelAccess, found: list[Member]) -> list[dict[str, Any]]:
    """Each member, whether this person may change them, and whether it is this person.

    A person never changes their own role from the list, and the last owner is not offered
    Leave, since the service refuses both (the channel would have no owner).
    """
    owners = sum(1 for member in found if member.role is Role.OWNER)
    rows = []
    for member in found:
        is_self = member.user_id == access.user.id
        is_last_owner = member.role is Role.OWNER and owners == 1
        rows.append(
            {
                "member": member,
                "can_change": not is_self and can_change(access, member),
                "is_self": is_self,
                "can_leave": is_self and not is_last_owner,
            }
        )
    return rows


def members_page(
    request: Request, app: Services, access: ChannelAccess, shown: MembersView
) -> Response:
    """The members, the pending invitations and the invitation form."""
    may_manage = access.can(Permission.MANAGE_MEMBERS)
    translate = translator(request)
    context = {
        "rows": member_rows(access, members(app, access)),
        "invitations": pending_invitations(app, access) if may_manage else [],
        "roles": grantable_roles(access),
        "role_choices": [
            (role.value, translate.role(role).capitalize()) for role in grantable_roles(access)
        ],
        "errors": shown.outcome.errors,
        "target": shown.target,
        "sent": shown.sent,
    }
    view = View(
        template="workspace/members.html",
        section="members",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/members")
def members_list(request: Request, app: ServicesDep, access: PageAccess) -> Response:
    """Who belongs to the channel, and with which role."""
    return members_page(request, app, access, MembersView())


def chosen_role(access: ChannelAccess, value: str) -> Role | None:
    """The role a form names, if this person may grant it."""
    allowed = {role.value: role for role in grantable_roles(access)}
    return allowed.get(value)


def invite_outcome(
    app: Services, access: ChannelAccess, fields: InviteFields, translate: Translator
) -> Outcome | None:
    """Send the invitation; the errors to show, or None when it is sent."""
    role = chosen_role(access, fields.role)
    if role is None:
        return Outcome.invalid({"role": translate(CHOOSE_ROLE)})
    try:
        data = Invite(email=fields.email.strip(), role=role)
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    emails = {member.email.casefold() for member in members(app, access)}
    if data.email.casefold() in emails:
        return Outcome.conflict({"email": translate(ALREADY_MEMBER)})
    try:
        invite(app, app.accounts(translate.locale), access, data)
    except ProblemError as problem:
        return refused(problem, translate)
    return None


@router.post("/app/channels/{slug}/members/invite")
def send_invitation(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[InviteFields, Form()],
) -> Response:
    """Invite someone by email; the email goes out in the page's language."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.MANAGE_MEMBERS)
    translate = translator(request)
    if within_rate_limit(request, app):
        outcome = invite_outcome(app, access, fields, translate)
    else:
        outcome = Outcome.refused({"email": translate(TOO_MANY_ATTEMPTS)}, TOO_MANY)
    if outcome is not None:
        shown = MembersView(outcome=outcome, target="invite", sent=fields)
        return members_page(request, app, access, shown)
    return done(request, app, channel_url(access.channel.slug, MEMBERS_SUFFIX), Flash.INVITED)


def member_in(app: Services, access: ChannelAccess, user_id: str) -> Member:
    """The member a path names; 404 when there is no such member."""
    try:
        wanted = UUID(user_id)
    except ValueError as malformed:
        raise PageNotFound(user_id) from malformed
    for member in members(app, access):
        if member.user_id == wanted:
            return member
    raise PageNotFound(user_id)


class RoleChange(BaseModel):
    """A member and the role a form names for them."""

    model_config = ConfigDict(frozen=True)

    member: Member
    value: str


def role_outcome(
    app: Services, access: ChannelAccess, change: RoleChange, translate: Translator
) -> Outcome | None:
    """Change the member's role; the error to show, or None when it is changed."""
    role = chosen_role(access, change.value)
    if role is None:
        return Outcome.invalid({"role": translate(CHOOSE_ROLE)})
    try:
        change_role(app, access, change.member.user_id, role)
    except MembershipError as refusal:
        return Outcome.conflict({"role": translate(refusal.message)})
    return None


@router.post("/app/channels/{slug}/members/{user_id}/role")
def set_role(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    user_id: str,
    fields: Annotated[RoleFields, Form()],
) -> Response:
    """Change a member's role."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    member = member_in(app, access, user_id)
    if not can_change(access, member):
        raise PageForbidden(user_id)
    change = RoleChange(member=member, value=fields.role)
    outcome = role_outcome(app, access, change, translator(request))
    if outcome is not None:
        shown = MembersView(outcome=outcome, target=user_id)
        return members_page(request, app, access, shown)
    return done(request, app, channel_url(access.channel.slug, MEMBERS_SUFFIX), Flash.SAVED)


@router.post("/app/channels/{slug}/members/{user_id}/remove")
def remove(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    user_id: str,
    fields: Annotated[SubmittedFields, Form()],
) -> Response:
    """Remove a member, or leave the channel when it is the person themself."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    member = member_in(app, access, user_id)
    is_self = member.user_id == access.user.id
    if not is_self and not can_change(access, member):
        raise PageForbidden(user_id)
    try:
        remove_member(app, access, member.user_id)
    except MembershipError as refusal:
        outcome = Outcome.conflict({"form": translator(request)(refusal.message)})
        return members_page(request, app, access, MembersView(outcome=outcome, target=user_id))
    if is_self:
        return done(request, app, APP_PATH, Flash.LEFT)
    return done(request, app, channel_url(access.channel.slug, MEMBERS_SUFFIX), Flash.REMOVED)


@router.post("/app/channels/{slug}/invitations/{invitation_id}/revoke")
def revoke(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    invitation_id: str,
    fields: Annotated[SubmittedFields, Form()],
) -> Response:
    """Withdraw a pending invitation; its link stops working."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.MANAGE_MEMBERS)
    pending = {str(item.id): item for item in pending_invitations(app, access)}
    invitation = pending.get(invitation_id)
    if invitation is None:
        raise PageNotFound(invitation_id)
    if invitation.role not in grantable_roles(access):
        raise PageForbidden(invitation_id)
    try:
        revoke_invitation(app, access, invitation.id)
    except MembershipError as refusal:
        outcome = Outcome.conflict({"form": translator(request)(refusal.message)})
        return members_page(request, app, access, MembersView(outcome=outcome))
    return done(request, app, channel_url(access.channel.slug, MEMBERS_SUFFIX), Flash.REVOKED)
