"""The person's own account: their name, their password and their API tokens.

A new token's secret is shown once, on the page that answers its creation, and never stored or
shown again (rule agentic.tokens.scoped); that page is not cached.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor import operations
from sreditor.deps import Services, ServicesDep, start_session, within_rate_limit
from sreditor.i18n import N_, Translator
from sreditor.pagination import DEFAULT_LIMIT, decode_cursor, page
from sreditor.problems import ProblemError
from sreditor.profile import PasswordChange, ProfileError, change_password, rename
from sreditor.tokens import DEFAULT_TTL_DAYS, NewToken, Scope, TokenInput
from sreditor.users import DisplayName, User
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.labels import SCOPE_HINTS
from sreditor.web.routes import TOO_MANY, TOO_MANY_ATTEMPTS, rejected_form, translator
from sreditor.web.workspace.forms import (
    TOKEN_DAYS,
    PasswordFields,
    ProfileFields,
    TokenFields,
)
from sreditor.web.workspace.shell import (
    APP_PATH,
    Flash,
    Outcome,
    PageNotFound,
    PageUser,
    done,
    workspace_page,
)

router = APIRouter()

ACCOUNT_PATH = f"{APP_PATH}/account"
TOKENS_PATH = f"{ACCOUNT_PATH}/tokens"
CHOOSE_SCOPE = N_("Choose at least one permission.")
CHOOSE_DAYS = N_("Choose how long the token lasts.")
CHOOSE_YOUR_CHANNELS = N_("Choose only channels you belong to.")
DAYS = N_("%(count)s days")
# A page with a secret on it must not stay in any cache.
NO_STORE = {"Cache-Control": "no-store"}


class NameCheck(BaseModel):
    """A display name, validated as the account stores it."""

    model_config = ConfigDict(frozen=True)

    display_name: DisplayName


def account_page(request: Request, app: Services, user: User, outcome: Outcome) -> Response:
    """The profile and password forms."""
    return workspace_page(
        request,
        app,
        user,
        "workspace/account.html",
        status_code=outcome.status_code,
        errors=outcome.errors,
    )


@router.get(ACCOUNT_PATH)
def account(request: Request, app: ServicesDep, user: PageUser) -> Response:
    """The person's name and password."""
    return account_page(request, app, user, Outcome())


@router.post(f"{ACCOUNT_PATH}/profile")
def save_profile(
    request: Request, app: ServicesDep, user: PageUser, fields: Annotated[ProfileFields, Form()]
) -> Response:
    """Change how the person's name is shown."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    try:
        checked = NameCheck(display_name=fields.display_name)
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translator(request)))
        return account_page(request, app, user, outcome)
    rename(app, user, checked.display_name)
    return done(request, app, ACCOUNT_PATH, Flash.SAVED)


def password_outcome(
    app: Services, user: User, fields: PasswordFields, translate: Translator
) -> Outcome | None:
    """Change the password; the errors to show, or None when it is changed."""
    try:
        change = PasswordChange(current_password=fields.current_password, password=fields.password)
        change_password(app, user, change)
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except ProfileError as refusal:
        return Outcome.invalid({"current_password": translate(refusal.message)})
    return None


@router.post(f"{ACCOUNT_PATH}/password")
def save_password(
    request: Request, app: ServicesDep, user: PageUser, fields: Annotated[PasswordFields, Form()]
) -> Response:
    """Change the password; every other session is signed out, this one starts again."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    translate = translator(request)
    if within_rate_limit(request, app):
        outcome = password_outcome(app, user, fields, translate)
    else:
        busy = {"current_password": translate(TOO_MANY_ATTEMPTS)}
        outcome = Outcome.refused(busy, TOO_MANY)
    if outcome is not None:
        return account_page(request, app, user, outcome)
    response = done(request, app, ACCOUNT_PATH, Flash.PASSWORD)
    start_session(app, user, request, response)
    return response


class TokensView(BaseModel):
    """The tokens page: errors and values of the form sent, and a secret just created."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome = Outcome()
    sent: TokenFields | None = None
    created: NewToken | None = None
    cursor: str = ""


def channel_choices(app: Services, user: User) -> list[tuple[str, str]]:
    """(slug, name) of each channel the person belongs to, for limiting a token to some."""
    return [(slug, app.channels.get(slug).name) for slug in sorted(app.members.roles_of(user.id))]


def tokens_page(request: Request, app: Services, user: User, shown: TokensView) -> Response:
    """The person's tokens, a page at a time, and the form for a new one."""
    rows = app.tokens.list(
        user.id, after=decode_cursor(shown.cursor or None), limit=DEFAULT_LIMIT + 1
    )
    found = page(rows, DEFAULT_LIMIT, lambda token: str(token.id))
    translate = translator(request)
    context: dict[str, Any] = {
        "tokens": found.items,
        "next_cursor": found.next_cursor,
        "scope_choices": [
            (scope.value, scope.value, translate(SCOPE_HINTS[scope])) for scope in Scope
        ],
        "channel_choices": channel_choices(app, user),
        "day_choices": [(str(days), translate(DAYS, count=days)) for days in TOKEN_DAYS],
        "default_days": DEFAULT_TTL_DAYS,
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        "created": shown.created,
    }
    response = workspace_page(
        request,
        app,
        user,
        "workspace/tokens.html",
        status_code=shown.outcome.status_code,
        **context,
    )
    response.headers.update(NO_STORE)
    return response


@router.get(TOKENS_PATH)
def tokens(request: Request, app: ServicesDep, user: PageUser, cursor: str = "") -> Response:
    """The person's API tokens, newest first."""
    return tokens_page(request, app, user, TokensView(cursor=cursor))


def token_input(fields: TokenFields, translate: Translator) -> TokenInput | dict[str, str]:
    """The token the form describes, or the errors to show."""
    known = {scope.value for scope in Scope}
    scopes = [Scope(value) for value in fields.scopes if value in known]
    if not scopes:
        return {"scopes": translate(CHOOSE_SCOPE)}
    if not fields.expires_in_days.isdigit():
        return {"expires_in_days": translate(CHOOSE_DAYS)}
    try:
        return TokenInput(
            name=fields.name,
            scopes=scopes,
            expires_in_days=int(fields.expires_in_days),
            channels=fields.channels or None,
        )
    except ValidationError as invalid:
        return field_errors(invalid, translate)


@router.post(TOKENS_PATH)
def create_token(
    request: Request, app: ServicesDep, user: PageUser, fields: Annotated[TokenFields, Form()]
) -> Response:
    """Create a token and show its secret, this once."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    data = token_input(fields, translator(request))
    if isinstance(data, dict):
        shown = TokensView(outcome=Outcome.invalid(data), sent=fields)
        return tokens_page(request, app, user, shown)
    try:
        created = operations.create_token(app, user, data)
    except ProblemError:
        errors = {"channels": translator(request)(CHOOSE_YOUR_CHANNELS)}
        return tokens_page(
            request, app, user, TokensView(outcome=Outcome.invalid(errors), sent=fields)
        )
    return tokens_page(request, app, user, TokensView(created=created))


@router.post(f"{TOKENS_PATH}/{{token_id}}/revoke")
def revoke_token(
    request: Request,
    app: ServicesDep,
    user: PageUser,
    token_id: str,
    fields: Annotated[SubmittedFields, Form()],
) -> Response:
    """Revoke a token; requests made with it fail from now on."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    try:
        wanted = UUID(token_id)
    except ValueError as malformed:
        raise PageNotFound(token_id) from malformed
    if not app.tokens.revoke(user.id, wanted):
        raise PageNotFound(token_id)
    return done(request, app, TOKENS_PATH, Flash.REVOKED)
