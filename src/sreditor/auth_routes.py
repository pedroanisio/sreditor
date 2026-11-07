"""Registration, email verification, sign-in, password reset and the signed-in user's profile."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from sreditor import profile
from sreditor.accounts import AccountError
from sreditor.deps import (
    SESSION_COOKIE,
    AccountsDep,
    CurrentUser,
    ServicesDep,
    SessionUser,
    rate_limit,
    refused,
    request_token,
    session_token,
    start_session,
)
from sreditor.field_types import EmailToken
from sreditor.members import Role
from sreditor.operations import require_scope
from sreditor.profile import PasswordChange, WrongCurrentPassword
from sreditor.tokens import ApiToken, Scope
from sreditor.users import DisplayName, Password, User

router = APIRouter(tags=["auth"])
limited = [Depends(rate_limit)]
FROZEN = ConfigDict(frozen=True)


class RegisterInput(BaseModel):
    """A new account: email, display name and password."""

    model_config = FROZEN

    email: EmailStr
    display_name: DisplayName
    password: Password


class LoginInput(BaseModel):
    """Email and password to sign in with."""

    model_config = FROZEN

    email: EmailStr
    password: Annotated[str, Field(max_length=256)]


class EmailTokenInput(BaseModel):
    """A one-time token from an email link."""

    model_config = FROZEN

    token: EmailToken


class EmailInput(BaseModel):
    """An email address."""

    model_config = FROZEN

    email: EmailStr


class ResetInput(BaseModel):
    """A password-reset token and the new password."""

    model_config = FROZEN

    token: EmailToken
    password: Password


class ProfileInput(BaseModel):
    """Profile changes; a new password needs the current one."""

    model_config = FROZEN

    display_name: DisplayName | None = None
    password: Password | None = None
    current_password: Annotated[str, Field(max_length=256)] | None = None


class Accepted(BaseModel):
    """The request was taken; the outcome arrives by email."""

    model_config = FROZEN

    status: str


class SignedIn(BaseModel):
    """A new session token and its user."""

    model_config = FROZEN

    token: str
    user: User


class Me(BaseModel):
    """The signed-in user and their role on each channel."""

    model_config = FROZEN

    user: User
    channels: dict[str, Role]


def profile_of(app: ServicesDep, user: User, token: ApiToken | None = None) -> Me:
    """The user with their channel roles, only those a limited token reaches."""
    roles = app.members.roles_of(user.id)
    if token is not None:
        roles = {slug: role for slug, role in roles.items() if token.reaches(slug)}
    return Me(user=user, channels=roles)


@router.post(
    "/auth/register",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=limited,
    summary="Register an account",
)
def register(data: RegisterInput, accounts: AccountsDep) -> Accepted:
    """Always answers the same way, so it cannot reveal which emails are registered."""
    accounts.register(data.email, data.display_name, data.password)
    return Accepted(status="verification_sent")


@router.post("/auth/verify", dependencies=limited, summary="Verify an email address")
def verify(data: EmailTokenInput, accounts: AccountsDep) -> User:
    """Verify an email address with the token from the verification email."""
    try:
        return accounts.verify_email(data.token)
    except AccountError as refusal:
        raise refused(refusal) from refusal


@router.post("/auth/login", dependencies=limited, summary="Sign in")
def login(
    data: LoginInput,
    request: Request,
    response: Response,
    app: ServicesDep,
    accounts: AccountsDep,
) -> SignedIn:
    """Sign in with email and password; returns a session token and sets its cookie."""
    try:
        user = accounts.authenticate(data.email, data.password)
    except AccountError as refusal:
        raise refused(refusal) from refusal
    return SignedIn(token=start_session(app, user, request, response), user=user)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
def logout(request: Request, app: ServicesDep) -> Response:
    """End the current session and clear its cookie."""
    token = session_token(request)
    if token:
        app.users.delete_session(token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=app.settings.has_secure_cookies)
    return response


@router.post(
    "/auth/password-reset",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=limited,
    summary="Request a password reset",
)
def request_password_reset(data: EmailInput, accounts: AccountsDep) -> Accepted:
    """Always answers the same way, so it cannot reveal which emails are registered."""
    accounts.request_password_reset(data.email)
    return Accepted(status="reset_sent")


@router.post(
    "/auth/password-reset/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=limited,
    summary="Set a new password",
)
def confirm_password_reset(data: ResetInput, accounts: AccountsDep) -> Response:
    """Set the new password, sign out every session, and verify the email if it was pending."""
    try:
        accounts.reset_password(data.token, data.password)
    except AccountError as refusal:
        raise refused(refusal) from refusal
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", summary="Read the signed-in user")
def get_me(request: Request, user: CurrentUser, app: ServicesDep) -> Me:
    """The signed-in user and their role on each channel.

    It lists channels, so a token needs channels:read, as list_channels does.
    """
    token = request_token(request)
    require_scope(token, Scope.CHANNELS_READ)
    return profile_of(app, user, token)


@router.patch("/me", summary="Update your profile")
def update_me(data: ProfileInput, user: SessionUser, app: ServicesDep) -> Me:
    """Rename, or change the password. A password change signs out every session.

    Needs a signed-in session: an API token cannot change its user's profile.
    """
    if data.password is not None:
        change = PasswordChange(
            current_password=data.current_password or "", password=data.password
        )
        try:
            profile.change_password(app, user, change)
        except WrongCurrentPassword as refusal:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(refusal)) from refusal
    if data.display_name is not None:
        user = profile.rename(app, user, data.display_name)
    return profile_of(app, user)
