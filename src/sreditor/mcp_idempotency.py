"""Idempotency keys for MCP tools that create, upload or spend (rule agentic.mcp.idempotency).

One helper serves every keyed tool, with the API's rules (agentic.idempotency): a repeat with the
same key and arguments answers with the first result, marked `sreditor/replayed` in the result's
`_meta`; the same key with other arguments is refused; keys expire after 24 hours and are never
shared between users.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field

from sreditor.deps import Services
from sreditor.idempotency import MAX_KEY_LENGTH, Idempotency, request_hash
from sreditor.idempotency_store import IdempotencyStore
from sreditor.mcp_principal import principal

MCP_METHOD = "MCP"
REPLAYED_META = "sreditor/replayed"
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
    arguments = call.arguments.model_dump(mode="json") if call.arguments is not None else {}
    body = json.dumps(arguments, sort_keys=True).encode()
    fingerprint = request_hash(MCP_METHOD, call.operation, body)
    store = IdempotencyStore(services.users.database)
    once = Idempotency(store, principal(context).user.id, call.key, fingerprint)
    outcome, was_replayed = once.attempt(produce, model)
    return tool_result(outcome, was_replayed if call.key is not None else None)
