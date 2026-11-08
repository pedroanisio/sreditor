"""OAuth 2.1 for MCP clients (rules agentic.oauth and agentic.oauth.protocol).

sreditor is its own authorization server. A client registers itself, sends the person to the
consent page, and trades the single-use code (with its PKCE verifier) for an ordinary `sre_`
API token, so every rule that applies to tokens applies to OAuth clients too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Protocol
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from sreditor.database import (
    OAUTH_TOKEN_CLIENT_NAME_LENGTH,
    OAUTH_TOKEN_NAME_SUFFIX,
    Database,
    stamp,
)
from sreditor.security import hash_token
from sreditor.tokens import DEFAULT_TTL_DAYS, NewToken, Scope, TokenInput, TokenStore

CODE_TTL = timedelta(minutes=10)
CLIENT_ID_BYTES = 16
CODE_BYTES = 32
PKCE_METHOD = "S256"
RESPONSE_TYPE = "code"
GRANT_TYPE = "authorization_code"
AUTH_METHOD = "none"  # public clients: PKCE, not a client secret, proves who redeems a code
HTTPS = "https"
# Registered client ids are random URL-safe strings, so an https URL is always a CIMD client.
CLIENT_ID_URL_PREFIX = "https://"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Caps on what an anonymous caller may register (rule agentic.oauth.protocol); real clients
# (claude.ai, ChatGPT, Claude Code) send a name and one to three short redirect URIs.
MAX_REGISTRATION_BYTES = 32 * 1024  # room for ten redirect URIs at their full length
MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI_LENGTH = 2000
MAX_CLIENT_NAME_LENGTH = 200
# A client nobody has approved a connection for in this long is purged.
UNUSED_CLIENT_TTL = timedelta(days=30)
DEFAULT_CLIENT_NAME = "MCP client"
# RFC 7636: 43 to 128 characters from the unreserved set.
VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
# Scopes preselected on the consent page when a client asks for none it can name; deleting
# channels and reading waivers (personal data, rule project.waivers) must be picked by hand.
UNSELECTED_SCOPES = frozenset({Scope.CHANNELS_DELETE, Scope.WAIVERS_READ})
DEFAULT_SCOPES = tuple(scope for scope in Scope if scope not in UNSELECTED_SCOPES)


class ErrorCode(StrEnum):
    """RFC 6749 and RFC 7591 error codes."""

    INVALID_REQUEST = "invalid_request"
    INVALID_CLIENT = "invalid_client"
    INVALID_GRANT = "invalid_grant"
    INVALID_SCOPE = "invalid_scope"
    INVALID_TARGET = "invalid_target"
    UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type"
    UNSUPPORTED_RESPONSE_TYPE = "unsupported_response_type"
    ACCESS_DENIED = "access_denied"
    INVALID_REDIRECT_URI = "invalid_redirect_uri"
    INVALID_CLIENT_METADATA = "invalid_client_metadata"


class OAuthError(Exception):
    """A refusal in OAuth's own terms; `can_redirect` when the client may be told by redirect."""

    def __init__(self, code: ErrorCode, description: str, can_redirect: bool = False) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.can_redirect = can_redirect


def is_allowed_redirect(uri: str) -> bool:
    """https anywhere, or http on this machine (agents like Claude Code listen on loopback)."""
    parts = urlsplit(uri)
    if parts.fragment or not parts.hostname:
        return False
    return parts.scheme == HTTPS or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)


def is_registered_redirect(uri: str, registered: list[str]) -> bool:
    """Whether a client registered this redirect URI (rule agentic.oauth.protocol).

    URIs match exactly, except that a loopback http one matches on any port: native clients
    such as Claude Code listen on a free port chosen at each sign-in (RFC 8252 section 7.3).
    """
    if uri in registered:
        return True
    parts = urlsplit(uri)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        return False
    return any(without_port(uri) == without_port(candidate) for candidate in registered)


def without_port(uri: str) -> str:
    """The URI with its port removed, so loopback redirects compare on everything else."""
    parts = urlsplit(uri)
    host = f"[{parts.hostname}]" if parts.hostname and ":" in parts.hostname else parts.hostname
    return parts._replace(netloc=host or "").geturl()


ClientName = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=MAX_CLIENT_NAME_LENGTH)
]


class ClientRegistration(BaseModel):
    """RFC 7591 client metadata; fields sreditor does not use are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    redirect_uris: Annotated[
        list[Annotated[str, StringConstraints(max_length=MAX_REDIRECT_URI_LENGTH)]],
        Field(min_length=1, max_length=MAX_REDIRECT_URIS),
    ]
    client_name: ClientName = DEFAULT_CLIENT_NAME
    token_endpoint_auth_method: str = AUTH_METHOD

    @field_validator("redirect_uris")
    @classmethod
    def redirects_allowed(cls, uris: list[str]) -> list[str]:
        """Refuse redirect URIs that are not https or loopback http."""
        refused = [uri for uri in uris if not is_allowed_redirect(uri)]
        if refused:
            raise ValueError(f"Redirect URIs must be https or loopback http: {', '.join(refused)}")
        return uris

    @field_validator("token_endpoint_auth_method")
    @classmethod
    def public_client(cls, method: str) -> str:
        """Only public clients: PKCE stands in for a client secret."""
        if method != AUTH_METHOD:
            raise ValueError("Only public clients with PKCE are supported (auth method 'none')")
        return method


class OAuthClient(BaseModel):
    """A client sreditor knows, registered or described by its metadata document."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    client_name: str
    redirect_uris: list[str]
    created_at: datetime


class AuthorizationRequest(BaseModel):
    """The query of /oauth/authorize, carried through the consent form unchanged."""

    model_config = ConfigDict(frozen=True)

    client_id: str = ""
    redirect_uri: str = ""
    response_type: str = ""
    code_challenge: str = ""
    code_challenge_method: str = ""
    state: str = ""
    scope: str = ""
    resource: str = ""


class Grant(BaseModel):
    """What the person approved, waiting for the client to redeem its code."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    user_id: UUID
    redirect_uri: str
    code_challenge: str
    scopes: list[Scope]


class TokenRequest(BaseModel):
    """The form a client posts to /oauth/token."""

    model_config = ConfigDict(frozen=True)

    grant_type: str = ""
    code: str = ""
    redirect_uri: str = ""
    client_id: str = ""
    code_verifier: str = ""


def requested_scopes(scope: str) -> list[Scope]:
    """The scopes a client asked for that sreditor knows, or the default set."""
    known = {item.value: item for item in Scope}
    asked = [known[name] for name in scope.split() if name in known]
    return asked or list(DEFAULT_SCOPES)


def challenge_of(verifier: str) -> str:
    """The S256 code challenge of a PKCE verifier (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verifier_matches(verifier: str, challenge: str) -> bool:
    """Whether the PKCE verifier hashes to the challenge sent with the request."""
    return bool(VERIFIER.fullmatch(verifier)) and secrets.compare_digest(
        challenge_of(verifier), challenge
    )


def redirect_with(uri: str, params: dict[str, str]) -> str:
    """The client's redirect URI with the answer added to its query."""
    present = {key: value for key, value in params.items() if value}
    return uri + ("&" if urlsplit(uri).query else "?") + urlencode(present)


def resources_for(public_url: str, mcp_path: str) -> set[str]:
    """Resource indicators (RFC 8707) that name this server."""
    base = public_url.rstrip("/")
    return {base, base + "/", base + mcp_path, base + mcp_path + "/"}


class ClientDocumentSource(Protocol):
    """Where clients that name themselves by a URL are looked up (rule agentic.oauth.cimd)."""

    def client(self, url: str) -> OAuthClient:
        """The client a client_id URL names; OAuthError when its document is unusable."""
        ...


class OAuthStore:
    """Registered clients and pending codes, and the exchange of a code for an API token."""

    def __init__(
        self,
        database: Database,
        tokens: TokenStore,
        documents: ClientDocumentSource | None = None,
    ) -> None:
        self.database = database
        self.tokens = tokens
        self.documents = documents

    def purge_expired_codes(self) -> None:
        """Delete codes past their expiry; a code that was used is deleted when redeemed."""
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM oauth_codes WHERE expires_at <= ?", (self.database.now(),)
            )

    def purge_stale(self) -> None:
        """Delete expired codes and clients nobody used within UNUSED_CLIENT_TTL.

        A client that was ever approved keeps its row: tokens do not depend on it, but a client
        that registered once and re-authorizes later must still be known. Only registrations
        nobody used, which is all an anonymous caller can create, go away.
        """
        self.purge_expired_codes()
        cutoff = stamp(self.database.clock() - UNUSED_CLIENT_TTL)
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM oauth_clients WHERE last_used_at IS NULL AND created_at < ?",
                (cutoff,),
            )

    def register(self, registration: ClientRegistration) -> OAuthClient:
        """Register a client (RFC 7591) and give it a fresh client_id."""
        # Registration is open to anyone, so it is where stale rows get cleaned up.
        self.purge_stale()
        client = OAuthClient(
            client_id=secrets.token_urlsafe(CLIENT_ID_BYTES),
            client_name=registration.client_name or DEFAULT_CLIENT_NAME,
            redirect_uris=registration.redirect_uris,
            created_at=self.database.clock().replace(microsecond=0),
        )
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    client.client_id,
                    client.client_name,
                    json.dumps(client.redirect_uris),
                    stamp(client.created_at),
                ),
            )
        return client

    def client(self, client_id: str) -> OAuthClient | None:
        """A registered client, or the one a client_id URL's metadata document describes."""
        if client_id.startswith(CLIENT_ID_URL_PREFIX) and self.documents is not None:
            return self.documents.client(client_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        if row is None:
            return None
        return OAuthClient(
            client_id=row["client_id"],
            client_name=row["client_name"],
            redirect_uris=json.loads(row["redirect_uris"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def check(self, request: AuthorizationRequest, resources: set[str]) -> OAuthClient:
        """The client of a valid authorization request; refusals say whether to redirect."""
        client = self.client(request.client_id)
        if client is None:
            raise OAuthError(ErrorCode.INVALID_CLIENT, "Unknown client")
        if not is_registered_redirect(request.redirect_uri, client.redirect_uris):
            raise OAuthError(ErrorCode.INVALID_REQUEST, "redirect_uri is not registered")
        check_request_details(request, resources)
        return client

    def remember(self, client: OAuthClient) -> None:
        """Keep a document client on record, so its codes can refer to it like any other."""
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, created_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT (client_id) DO UPDATE SET"
                " client_name = excluded.client_name, redirect_uris = excluded.redirect_uris",
                (
                    client.client_id,
                    client.client_name,
                    json.dumps(client.redirect_uris),
                    stamp(client.created_at),
                ),
            )

    def issue_code(self, grant: Grant) -> str:
        """A single-use code for the grant; only its hash is stored."""
        code = secrets.token_urlsafe(CODE_BYTES)
        self.purge_expired_codes()
        expires_at = stamp(self.database.clock() + CODE_TTL)
        if grant.client_id.startswith(CLIENT_ID_URL_PREFIX):
            client = self.client(grant.client_id)
            if client is not None:
                self.remember(client)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?",
                (self.database.now(), grant.client_id),
            )
            connection.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, user_id, redirect_uri,"
                " code_challenge, scopes, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    hash_token(code),
                    grant.client_id,
                    str(grant.user_id),
                    grant.redirect_uri,
                    grant.code_challenge,
                    " ".join(grant.scopes),
                    expires_at,
                ),
            )
        return code

    def redeem(self, request: TokenRequest) -> Grant:
        """Spend a code: it works once, before it expires, for the client it was issued to."""
        with self.database.connect() as connection:
            row = connection.execute(
                "DELETE FROM oauth_codes WHERE code_hash = ? AND expires_at > ? RETURNING *",
                (hash_token(request.code), self.database.now()),
            ).fetchone()
        if row is None:
            raise OAuthError(ErrorCode.INVALID_GRANT, "The code is unknown, used or expired")
        grant = Grant(
            client_id=row["client_id"],
            user_id=UUID(row["user_id"]),
            redirect_uri=row["redirect_uri"],
            code_challenge=row["code_challenge"],
            scopes=[Scope(name) for name in row["scopes"].split()],
        )
        check_redemption(grant, request)
        return grant

    def exchange(self, request: TokenRequest) -> NewToken:
        """The API token for a valid code (RFC 6749 section 4.1.3)."""
        if request.grant_type != GRANT_TYPE:
            raise OAuthError(ErrorCode.UNSUPPORTED_GRANT_TYPE, "Use authorization_code")
        grant = self.redeem(request)
        client = self.client(grant.client_id)
        name = (client.client_name if client else DEFAULT_CLIENT_NAME)[
            :OAUTH_TOKEN_CLIENT_NAME_LENGTH
        ]
        data = TokenInput(
            name=name + OAUTH_TOKEN_NAME_SUFFIX,
            scopes=grant.scopes,
            expires_in_days=DEFAULT_TTL_DAYS,
        )
        return self.tokens.create(grant.user_id, data)


def check_request_details(request: AuthorizationRequest, resources: set[str]) -> None:
    """Checks whose failure the client can be told about by redirect."""
    if request.response_type != RESPONSE_TYPE:
        raise OAuthError(ErrorCode.UNSUPPORTED_RESPONSE_TYPE, "Use response_type=code", True)
    if request.code_challenge_method != PKCE_METHOD or not request.code_challenge:
        raise OAuthError(ErrorCode.INVALID_REQUEST, "PKCE with S256 is required", True)
    if request.resource and request.resource not in resources:
        raise OAuthError(ErrorCode.INVALID_TARGET, "resource is not this server", True)


def check_redemption(grant: Grant, request: TokenRequest) -> None:
    """The code goes back to the client, redirect URI and verifier it was issued for."""
    if grant.client_id != request.client_id:
        raise OAuthError(ErrorCode.INVALID_GRANT, "The code was issued to another client")
    if grant.redirect_uri != request.redirect_uri:
        raise OAuthError(ErrorCode.INVALID_GRANT, "redirect_uri does not match")
    if not verifier_matches(request.code_verifier, grant.code_challenge):
        raise OAuthError(ErrorCode.INVALID_GRANT, "code_verifier does not match")
