"""Idempotency keys for MCP tools that create, upload or spend (rule agentic.mcp.idempotency).

One helper serves every keyed tool, with the API's rules (agentic.idempotency): a repeat with the
same key and arguments answers with the first result, marked `sreditor/replayed` in the result's
`_meta`; the same key with other arguments is refused; keys expire after 24 hours and are never
shared between users or between one user's tokens.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field

from sreditor.deps import Services
from sreditor.idempotency import MAX_KEY_LENGTH, Idempotency, caller_key, request_hash
from sreditor.idempotency_store import IdempotencyStore
from sreditor.mcp_principal import principal

MCP_METHOD = "MCP"
REPLAYED_META = "sreditor/replayed"
# What a stored link result keeps in place of its secret URL; a replay issues a new link.
REISSUED_URL = "reissued-on-replay"
IdempotencyKey = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=MAX_KEY_LENGTH,
        description=(
            "Any unique string per intended operation. A retry with the same key and arguments "
            "returns the first result, marked replayed, instead of doing it twice."
        ),
    ),
]


class IdempotentCall(BaseModel):
    """A call's name and arguments, which a retry with the same key must repeat exactly."""

    model_config = ConfigDict(frozen=True)

    operation: str
    arguments: BaseModel | None = None
    key: str | None = None

    def with_key(self, key: str | None) -> IdempotentCall:
        """The same call under the caller's idempotency key."""
        return self.model_copy(update={"key": key})


def tool_result(result: BaseModel, replayed: bool | None) -> CallToolResult:
    """A structured tool result; `replayed` is None when the call carried no key."""
    meta = None if replayed is None else {REPLAYED_META: replayed}
    return CallToolResult(
        content=[TextContent(type="text", text=result.model_dump_json(by_alias=True))],
        structured_content=result.model_dump(mode="json", by_alias=True),
        meta=meta,
    )


def run_once[T: BaseModel](
    services: Services,
    context: Context,
    call: IdempotentCall,
    produce: Callable[[], T],
    model: type[T],
) -> CallToolResult:
    """Run the call at most once per idempotency key and mark a replay in the result."""
    outcome, was_replayed = idempotency_for(services, context, call).attempt(produce, model)
    return tool_result(outcome, was_replayed if call.key is not None else None)


def idempotency_for(services: Services, context: Context, call: IdempotentCall) -> Idempotency:
    """The call's key and fingerprint, bound to the store and to the caller's token."""
    arguments = call.arguments.model_dump(mode="json") if call.arguments is not None else {}
    body = json.dumps(arguments, sort_keys=True).encode()
    fingerprint = request_hash(MCP_METHOD, call.operation, body)
    store = IdempotencyStore(services.users.database)
    caller = principal(context)
    return Idempotency(store, caller.user.id, caller_key(call.key, caller.token.id), fingerprint)


def issue_link_once[T: BaseModel](
    services: Services,
    context: Context,
    call: IdempotentCall,
    issue: Callable[[], T],
    model: type[T],
) -> CallToolResult:
    """Issue a signed link at most once per key, without ever storing its secret.

    The stored result keeps no `url`, since a stored secret would outlive the link's purpose; a
    replay issues a fresh link to the same place, checking the role and scope again, so a retry
    after a timeout still gets a link that works.
    """
    issued: list[T] = []

    def first() -> T:
        issued.append(issue())
        return issued[0].model_copy(update={"url": REISSUED_URL})

    _, was_replayed = idempotency_for(services, context, call).attempt(first, model)
    offer = issued[0] if issued else issue()
    return tool_result(offer, was_replayed if call.key is not None else None)
