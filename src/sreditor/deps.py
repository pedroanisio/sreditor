"""Services shared by the routes, and the dependencies that authenticate and authorize."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status

from sreditor.accounts import (
    AccountDisabled,
    AccountError,
    Accounts,
    AlreadyAMember,
    EmailNotVerified,
    InvitationForAnotherEmail,
    InvitationNotFound,
    WrongCredentials,
)
from sreditor.assets import AssetStore
from sreditor.channels import Channel
from sreditor.editorial import EditorialService
from sreditor.episodes import EpisodeStore
from sreditor.i18n import BASE_LOCALE, Translator, negotiate
from sreditor.library import LibraryStore
from sreditor.mail import Mailer
from sreditor.media import MediaStore
from sreditor.members import MemberStore, Permission, Role, allows
from sreditor.pitches import PitchStore
from sreditor.repository import ChannelNotFound, ChannelRepository
from sreditor.research import ResearchStore
from sreditor.screenplay import ScreenplayStore
from sreditor.security import Passwords, RateLimiter
from sreditor.settings import AppSettings
from sreditor.store import Owner, StmtClient
from sreditor.tokens import SCOPE_FOR, ApiToken, TokenStore, is_api_token
from sreditor.users import User, UserStore
from sreditor.waivers import WaiverStore

SESSION_COOKIE = "sreditor_session"
LANG_COOKIE = "sreditor_lang"

# The HTTP status each account refusal maps to; any other refusal is 400.
ERROR_STATUS: dict[type[AccountError], int] = {
    WrongCredentials: status.HTTP_401_UNAUTHORIZED,
    EmailNotVerified: status.HTTP_403_FORBIDDEN,
    AccountDisabled: status.HTTP_403_FORBIDDEN,
    InvitationNotFound: status.HTTP_404_NOT_FOUND,
    InvitationForAnotherEmail: status.HTTP_403_FORBIDDEN,
    AlreadyAMember: status.HTTP_409_CONFLICT,
}


@dataclass
class Services:
    """The shared collaborators of the app, built once at startup and handed to each request."""

    settings: AppSettings
    channels: ChannelRepository
    episodes: EpisodeStore
    research: ResearchStore
    pitches: PitchStore
    screenplays: ScreenplayStore
    library: LibraryStore
    media: MediaStore
    waivers: WaiverStore
    users: UserStore
    members: MemberStore
    editorials: EditorialService
    passwords: Passwords
    mailer: Mailer
    limiter: RateLimiter
    agent_limiter: RateLimiter  # one count per API token, shared by the API and /mcp
    stmt: StmtClient
    tokens: TokenStore
    assets: AssetStore

    def accounts(self, locale: str = BASE_LOCALE) -> Accounts:
        """Account flows whose emails are written in this locale."""
        return Accounts(
            self.users,
            self.members,
            self.passwords,
            self.mailer,
            self.settings.public_url,
            Translator(locale),
        )

    @property
    def session_ttl(self) -> timedelta:
        """How long a new session lasts, from the session_days setting."""
        return timedelta(days=self.settings.session_days)


def services(request: Request) -> Services:
    """The app's shared services, as stored on the application at startup."""
    shared: Services = request.app.state.services
    return shared


ServicesDep = Annotated[Services, Depends(services)]


def request_locale(request: Request) -> str:
    """The ?lang= switch, then the remembered choice, then Accept-Language (i18n.negotiation)."""
    return negotiate(
        request.query_params.get("lang"),
        request.cookies.get(LANG_COOKIE),
        request.headers.get("accept-language", ""),
    )


def request_accounts(request: Request, app: ServicesDep) -> Accounts:
    """The account service, set to the language chosen for the request."""
    return app.accounts(request_locale(request))


AccountsDep = Annotated[Accounts, Depends(request_accounts)]


def session_token(request: Request) -> str | None:
    """The session token from an `Authorization: Bearer` header or the session cookie."""
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and credentials:
        return credentials
    return request.cookies.get(SESSION_COOKIE)


def sign_in_first() -> HTTPException:
    """The 401 problem for a request that needs a session or token and has none."""
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Sign in first", headers={"WWW-Authenticate": "Bearer"}
    )


def current_user(request: Request, app: ServicesDep) -> User:
    """The user behind a session or, for agents, a personal API token (agentic.tokens.scoped).

    The token, when one was used, is kept on `request.state.api_token` so later checks can
    narrow the user's role to the token's scopes.
    """
    credential = session_token(request)
    request.state.api_token = None
    if credential and is_api_token(credential):
        found = app.tokens.authenticate(credential)
        if found is None:
            raise sign_in_first()
        user, request.state.api_token = found
        wait = agent_retry_after(app, request.state.api_token)
        if wait is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                AGENT_LIMIT_DETAIL,
                headers={"Retry-After": str(wait)},  # agentic.rate-limits
            )
        return user
    signed_in = app.users.session_user(credential) if credential else None
    if signed_in is None:
        raise sign_in_first()
    return signed_in


CurrentUser = Annotated[User, Depends(current_user)]


def request_token(request: Request) -> ApiToken | None:
    """The API token that authenticated this request, if any."""
    token: ApiToken | None = getattr(request.state, "api_token", None)
    return token


def session_user(request: Request, user: CurrentUser) -> User:
    """A user signed in with a session: API tokens cannot manage tokens or the profile."""
    if request_token(request) is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This needs a signed-in session")
    return user


SessionUser = Annotated[User, Depends(session_user)]


def start_session(app: Services, user: User, request: Request, response: Response) -> str:
    """Create a session and set its httpOnly cookie on the response."""
    token = app.users.create_session(user.id, app.session_ttl, request.headers.get("user-agent"))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(app.session_ttl.total_seconds()),
        httponly=True,
        secure=app.settings.has_secure_cookies,
        samesite="lax",
    )
    return token


def refused(refusal: AccountError) -> HTTPException:
    """The HTTP status an account flow's refusal maps to."""
    code = ERROR_STATUS.get(type(refusal), status.HTTP_400_BAD_REQUEST)
    return HTTPException(code, str(refusal))


def author(user: User) -> Owner:
    """How a user is recorded as the author of stmt statements."""
    return Owner(type="user", id=str(user.id))


def limit_key(request: Request) -> str:
    """The limiter key for this client on this path."""
    client = request.client.host if request.client else "unknown"
    return f"{request.url.path}:{client}"


def within_rate_limit(request: Request, app: Services) -> bool:
    """Count this attempt against the client's limit for this path."""
    return app.limiter.allow(limit_key(request))


def retry_after_seconds(request: Request, app: Services) -> int:
    """Whole seconds until this client may try this path again (agentic.rate-limits)."""
    return max(1, math.ceil(app.limiter.retry_after(limit_key(request))))


AGENT_LIMIT_DETAIL = "Too many requests for this API token; wait for Retry-After seconds"


def agent_retry_after(app: Services, token: ApiToken) -> int | None:
    """Count a request against the token's limit; when it is over, the whole seconds to wait."""
    key = str(token.id)
    if app.agent_limiter.allow(key):
        return None
    return max(1, math.ceil(app.agent_limiter.retry_after(key)))


def rate_limit(request: Request, app: ServicesDep) -> None:
    """Refuse a client over its limit with 429 and the real seconds left in Retry-After."""
    if not within_rate_limit(request, app):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts; try again later",
            headers={"Retry-After": str(retry_after_seconds(request, app))},
        )


@dataclass
class ChannelAccess:
    """A person's access to one channel: their role, and the API token they act through, if any."""

    channel: Channel
    role: Role | None  # None for an admin who is not a member
    user: User
    token: ApiToken | None = None
    revision: int = 0

    def can(self, permission: Permission) -> bool:
        """Whether the user may do this; admins may do anything, members what their role allows."""
        return self.user.is_admin or allows(self.role, permission)

    def require(self, permission: Permission) -> None:
        """Refuse with 403 unless the role, and an API token's scopes, allow the permission."""
        if not self.can(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Your role cannot {permission.value}")
        scope = SCOPE_FOR[permission]
        if self.token is not None and not self.token.allows(scope):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"This token lacks the {scope} scope")


def access_for(app: Services, slug: str, user: User, token: ApiToken | None) -> ChannelAccess:
    """The channel and the user's role on it. Non-members get 404, so channels stay private."""
    role = app.members.role(slug, user.id)
    if role is None and not user.is_admin:
        raise ChannelNotFound(slug)
    channel, revision = app.channels.get_with_revision(slug)
    return ChannelAccess(channel=channel, role=role, user=user, token=token, revision=revision)


def channel_access(
    slug: str, request: Request, user: CurrentUser, app: ServicesDep
) -> ChannelAccess:
    """The caller's access to the channel in the path."""
    return access_for(app, slug, user, request_token(request))


Access = Annotated[ChannelAccess, Depends(channel_access)]
