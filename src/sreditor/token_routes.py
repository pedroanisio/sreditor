"""Personal API tokens for agents, managed from a signed-in session (agentic.tokens.scoped)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response, status

from sreditor.deps import ServicesDep, SessionUser
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page, decode_cursor, page
from sreditor.problems import ProblemError
from sreditor.tokens import ApiToken, NewToken, TokenInput

router = APIRouter(tags=["tokens"])


@router.post("/me/tokens", status_code=status.HTTP_201_CREATED, summary="Create an API token")
def create_token(data: TokenInput, user: SessionUser, app: ServicesDep) -> NewToken:
    """Create an API token for an agent, limited to the given scopes.

    The `secret` is returned only in this response; store it now. The token never grants more
    than your own role on a channel. It takes no Idempotency-Key: a replay could not return the
    secret again, so retry by creating a new token and revoking the unused one.
    """
    return app.tokens.create(user.id, data)


@router.get("/me/tokens", summary="List your API tokens")
def list_tokens(
    user: SessionUser, app: ServicesDep, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> Page[ApiToken]:
    """Your API tokens, newest first, without their secrets."""
    rows = app.tokens.list(user.id, after=decode_cursor(cursor), limit=limit + 1)
    return page(rows, limit, lambda token: str(token.id))


@router.delete(
    "/me/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API token",
)
def revoke_token(token_id: UUID, user: SessionUser, app: ServicesDep) -> Response:
    """Revoke one of your API tokens; requests made with it fail from now on."""
    if not app.tokens.revoke(user.id, token_id):
        raise ProblemError(status.HTTP_404_NOT_FOUND, "Token not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
