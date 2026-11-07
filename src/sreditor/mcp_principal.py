"""Who is behind an MCP request: the user and API token the bearer check authenticated."""

from __future__ import annotations

from dataclasses import dataclass

from mcp.server.mcpserver import Context

from sreditor.tokens import ApiToken
from sreditor.users import User

PRINCIPAL = "principal"


@dataclass(frozen=True)
class Principal:
    """The user and API token behind an MCP request."""

    user: User
    token: ApiToken


def principal(context: Context) -> Principal:
    """The principal that BearerTokens authenticated for this tool call."""
    found: Principal = context.request_context.request.state.principal  # type: ignore[union-attr]
    return found
