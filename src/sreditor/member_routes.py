"""Channel members and invitations."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sreditor import membership
from sreditor.accounts import AccountError
from sreditor.auth_routes import TokenInput
from sreditor.deps import (
    Access,
    AccountsDep,
    ServicesDep,
    SessionUser,
    rate_limit,
    refused,
)
from sreditor.etags import ANY_REVISION, WEAK_PREFIX, IfMatch
from sreditor.idempotency import IdempotencyDep
from sreditor.members import Invitation, Member, Role
from sreditor.membership import (
    InvitationNotPending,
    LastOwner,
    MemberChanged,
    MembershipError,
    NotAMember,
)
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page, page_after
from sreditor.problems import ProblemError

router = APIRouter(tags=["members"])


class RoleInput(BaseModel):
    """The role to give a member."""

    model_config = ConfigDict(frozen=True)

    role: Role


class ChannelRole(BaseModel):
    """A channel and the role someone holds in it."""

    model_config = ConfigDict(frozen=True)

    channel: str
    role: Role


# The rules live in membership.py; the API only chooses each refusal's status.
REFUSAL_STATUS: dict[type[MembershipError], int] = {
    LastOwner: status.HTTP_409_CONFLICT,
    NotAMember: status.HTTP_404_NOT_FOUND,
    InvitationNotPending: status.HTTP_404_NOT_FOUND,
}


def member_etag(role: Role) -> str:
    """A member's ETag: their role, the only part of a membership that changes."""
    return f'"{role.value}"'


def expected_role(if_match: str | None) -> Role | None:
    """The role an If-Match header names; None when absent or `*`."""
    if if_match is None or if_match.strip() == ANY_REVISION:
        return None
    value = if_match.strip().removeprefix(WEAK_PREFIX).strip('"')
    if value not in set(Role):
        raise ProblemError(
            status.HTTP_412_PRECONDITION_FAILED, "If-Match must be a member ETag from this API"
        )
    return Role(value)


def membership_refused(refusal: MembershipError) -> Exception:
    """The HTTP error for a refused member change, keeping its English message as detail."""
    if isinstance(refusal, MemberChanged):
        headers = {"ETag": member_etag(refusal.current)} if refusal.current else None
        return ProblemError(
            status.HTTP_412_PRECONDITION_FAILED, str(refusal), "stale-revision", headers
        )
    code = REFUSAL_STATUS.get(type(refusal), status.HTTP_403_FORBIDDEN)
    return HTTPException(code, str(refusal))


@router.get("/channels/{slug}/members", summary="List channel members")
def list_members(
    access: Access, app: ServicesDep, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> Page[Member]:
    """The channel's members and their roles, ordered by user id."""
    return page_after(
        membership.members(app, access), cursor, limit, lambda member: str(member.user_id)
    )


@router.get("/channels/{slug}/members/{user_id}", summary="Read a channel member")
def get_member(user_id: UUID, access: Access, app: ServicesDep, response: Response) -> Member:
    """One member and their role. Its ETag is the value to send as If-Match when changing it."""
    try:
        found = membership.member(app, access, user_id)
    except MembershipError as refusal:
        raise membership_refused(refusal) from refusal
    response.headers["ETag"] = member_etag(found.role)
    return found


@router.post(
    "/channels/{slug}/invitations",
    status_code=status.HTTP_201_CREATED,
    response_model=Invitation,
    summary="Invite someone to a channel",
)
def invite(
    data: membership.Invite,
    access: Access,
    app: ServicesDep,
    accounts: AccountsDep,
    idempotency: IdempotencyDep,
) -> JSONResponse:
    """Invite someone by email to join the channel with a role. Accepts an Idempotency-Key."""
    return idempotency.run(
        lambda: membership.invite(app, accounts, access, data), status.HTTP_201_CREATED
    )


@router.post(
    "/invitations/accept", dependencies=[Depends(rate_limit)], summary="Accept an invitation"
)
def accept_invitation(data: TokenInput, user: SessionUser, accounts: AccountsDep) -> ChannelRole:
    """Join a channel with the token from an invitation email. Needs a signed-in session.

    Joining a channel is the person's own decision, so an API token cannot accept for them.
    The token travels in the body, never the URL, so access logs do not record it.
    """
    try:
        invitation = accounts.accept_invitation(data.token, user)
    except AccountError as refusal:
        raise refused(refusal) from refusal
    return ChannelRole(channel=invitation.channel, role=invitation.role)


@router.patch("/channels/{slug}/members/{user_id}", summary="Change a member's role")
def change_role(
    user_id: UUID,
    data: RoleInput,
    access: Access,
    app: ServicesDep,
    if_match: IfMatch = None,
) -> ChannelRole:
    """Change a member's role; only owners grant or take the owner and editor roles.

    Send the member's ETag as If-Match to avoid overwriting a change you have not seen.
    """
    try:
        role = membership.change_role(app, access, user_id, data.role, expected_role(if_match))
    except MembershipError as refusal:
        raise membership_refused(refusal) from refusal
    return ChannelRole(channel=access.channel.slug, role=role)


@router.delete(
    "/channels/{slug}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member",
)
def remove_member(
    user_id: UUID, access: Access, app: ServicesDep, if_match: IfMatch = None
) -> Response:
    """Remove a member. Anyone may leave a channel, except its last owner. Accepts If-Match."""
    try:
        membership.remove_member(app, access, user_id, expected_role(if_match))
    except MembershipError as refusal:
        raise membership_refused(refusal) from refusal
    return Response(status_code=status.HTTP_204_NO_CONTENT)
