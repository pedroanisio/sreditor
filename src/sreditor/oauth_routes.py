"""OAuth endpoints for MCP clients: discovery, registration and the token exchange.

They speak OAuth's own formats (rule agentic.oauth.protocol): errors are RFC 6749
`{error, error_description}` and registration takes no Idempotency-Key.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from sreditor.client_metadata import ClientDocuments
from sreditor.deps import Services, ServicesDep, rate_limit
from sreditor.mcp_server import PATH as MCP_PATH
from sreditor.mcp_server import RESOURCE_METADATA_PATH
from sreditor.oauth import (
    AUTH_METHOD,
    GRANT_TYPE,
    PKCE_METHOD,
    RESPONSE_TYPE,
    ClientRegistration,
    ErrorCode,
    OAuthError,
    OAuthStore,
    TokenRequest,
)
from sreditor.tokens import Scope

router = APIRouter(tags=["oauth"])

PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REGISTER_PATH = "/oauth/register"
RESOURCE_NAME = "sreditor"
BEARER_TOKEN_TYPE = "Bearer"
MISSING = "missing"  # Pydantic's error type for an absent field
# Token answers carry a secret (RFC 6749 section 5.1).
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
limited = [Depends(rate_limit)]


# Fetched client metadata documents, cached across requests as their Cache-Control allows.
CLIENT_DOCUMENTS = ClientDocuments()


def oauth_store(app: Services) -> OAuthStore:
    """OAuth clients and codes live in the same database as the API tokens they become."""
    return OAuthStore(app.tokens.database, app.tokens, CLIENT_DOCUMENTS)


def base_url(app: Services) -> str:
    """The public URL without a trailing slash, as the OAuth issuer."""
    return app.settings.public_url.rstrip("/")


def oauth_error(
    refusal: OAuthError, status_code: int = status.HTTP_400_BAD_REQUEST
) -> JSONResponse:
    """An RFC 6749 error body."""
    body = {"error": refusal.code.value, "error_description": refusal.description}
    return JSONResponse(body, status_code=status_code, headers=NO_STORE)


def protected_resource(app: Services) -> dict[str, Any]:
    """RFC 9728 metadata: /mcp, and the server that issues its tokens."""
    return {
        "resource": base_url(app) + MCP_PATH,
        "authorization_servers": [base_url(app)],
        "scopes_supported": [scope.value for scope in Scope],
        "bearer_methods_supported": ["header"],
        "resource_name": RESOURCE_NAME,
    }


@router.get(PROTECTED_RESOURCE_PATH, summary="Protected resource metadata")
def get_protected_resource_metadata(app: ServicesDep) -> dict[str, Any]:
    """OAuth protected-resource metadata (RFC 9728) for the MCP server.

    MCP clients read it after a 401 from /mcp to find where to sign in.
    """
    return protected_resource(app)


@router.get(RESOURCE_METADATA_PATH, summary="MCP protected resource metadata")
def get_mcp_protected_resource_metadata(app: ServicesDep) -> dict[str, Any]:
    """The same protected-resource metadata, at the path-specific address RFC 9728 defines."""
    return protected_resource(app)


@router.get(AUTHORIZATION_SERVER_PATH, summary="Authorization server metadata")
def get_authorization_server_metadata(app: ServicesDep) -> dict[str, Any]:
    """OAuth authorization-server metadata (RFC 8414): endpoints and what they support."""
    base = base_url(app)
    return {
        "issuer": base,
        "authorization_endpoint": base + AUTHORIZE_PATH,
        "token_endpoint": base + TOKEN_PATH,
        "registration_endpoint": base + REGISTER_PATH,
        "scopes_supported": [scope.value for scope in Scope],
        "response_types_supported": [RESPONSE_TYPE],
        "grant_types_supported": [GRANT_TYPE],
        "code_challenge_methods_supported": [PKCE_METHOD],
        "token_endpoint_auth_methods_supported": [AUTH_METHOD],
        # Clients may name themselves by a metadata document URL (rule agentic.oauth.cimd).
        "client_id_metadata_document_supported": True,
        # Redirects carry iss, so a client can tell which server answered (RFC 9207).
        "authorization_response_iss_parameter_supported": True,
    }


def registration_from(raw: bytes) -> ClientRegistration:
    """Client metadata from the request body, refused in RFC 7591 terms."""
    try:
        return ClientRegistration.model_validate(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError) as unreadable:
        raise OAuthError(ErrorCode.INVALID_CLIENT_METADATA, "Send JSON") from unreadable
    except ValidationError as invalid:
        # A redirect URI that is present but not allowed has its own code (RFC 7591 3.2.2).
        bad_redirect = any(
            error["loc"][:1] == ("redirect_uris",) and error["type"] != MISSING
            for error in invalid.errors()
        )
        code = ErrorCode.INVALID_REDIRECT_URI if bad_redirect else ErrorCode.INVALID_CLIENT_METADATA
        messages = "; ".join(str(error["msg"]) for error in invalid.errors())
        raise OAuthError(code, messages) from invalid


@router.post(
    REGISTER_PATH,
    status_code=status.HTTP_201_CREATED,
    dependencies=limited,
    summary="Register an OAuth client",
)
async def register_oauth_client(request: Request, app: ServicesDep) -> JSONResponse:
    """Register an OAuth client (RFC 7591 dynamic registration), as MCP clients do first.

    Send `redirect_uris` (https, or http on a loopback address) and optionally `client_name`.
    Clients are public: they prove themselves with PKCE, so no secret is issued.
    """
    try:
        registration = registration_from(await request.body())
    except OAuthError as refusal:
        return oauth_error(refusal)
    client = oauth_store(app).register(registration)
    body = {
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created_at.timestamp()),
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "grant_types": [GRANT_TYPE],
        "response_types": [RESPONSE_TYPE],
        "token_endpoint_auth_method": AUTH_METHOD,
    }
    return JSONResponse(body, status_code=status.HTTP_201_CREATED)


@router.post(TOKEN_PATH, dependencies=limited, summary="Exchange a code for a token")
def exchange_oauth_code(form: Annotated[TokenRequest, Form()], app: ServicesDep) -> JSONResponse:
    """Trade an authorization code and its PKCE verifier for an API token (RFC 6749 4.1.3).

    The access token is an ordinary `sre_` API token with the scopes the person approved; it
    shows on their API tokens page, where they can revoke it.
    """
    try:
        issued = oauth_store(app).exchange(form)
    except OAuthError as refusal:
        return oauth_error(refusal)
    seconds = int((issued.token.expires_at - issued.token.created_at).total_seconds())
    body = {
        "access_token": issued.secret,
        "token_type": BEARER_TOKEN_TYPE,
        "expires_in": seconds,
        "scope": " ".join(issued.token.scopes),
    }
    return JSONResponse(body, headers=NO_STORE)
