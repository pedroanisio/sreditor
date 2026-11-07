"""Cursor pagination for list endpoints (rule agentic.pagination)."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Sequence
from typing import Annotated

from fastapi import Query, status
from pydantic import BaseModel, ConfigDict, Field

from sreditor.problems import ProblemError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

Limit = Annotated[
    int, Query(ge=1, le=MAX_LIMIT, description=f"Items per page, at most {MAX_LIMIT}.")
]
# The same bounds for MCP tools, which validate with Pydantic rather than FastAPI's Query.
ToolLimit = Annotated[
    int, Field(ge=1, le=MAX_LIMIT, description=f"Items per page, at most {MAX_LIMIT}.")
]
Cursor = Annotated[
    str | None, Query(description="The `next_cursor` of the previous page; omit for the first.")
]


class Page[T](BaseModel):
    """One page of a list endpoint, with the cursor of the next page when more items exist."""

    model_config = ConfigDict(frozen=True)

    items: list[T]
    next_cursor: str | None = None


def encode_cursor(position: str) -> str:
    """Turn a position into an opaque URL-safe cursor."""
    return base64.urlsafe_b64encode(position.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    """Read the position from a cursor; an invalid cursor is a 400 problem."""
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return base64.b64decode(padded, altchars=b"-_", validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as invalid:
        raise ProblemError(
            status.HTTP_400_BAD_REQUEST, "Invalid cursor", "invalid-cursor"
        ) from invalid


def page[T](rows: Sequence[T], limit: int, position: Callable[[T], str]) -> Page[T]:
    """Build a page from up to `limit + 1` rows; the extra row only signals that more exist."""
    items = list(rows[:limit])
    more = len(rows) > limit
    return Page[T](items=items, next_cursor=encode_cursor(position(items[-1])) if more else None)


def page_after[T](
    rows: Sequence[T], cursor: str | None, limit: int, position: Callable[[T], str]
) -> Page[T]:
    """Page through rows already held in memory, ordered by `position`, after the cursor."""
    after = decode_cursor(cursor)
    ordered = sorted(rows, key=position)
    remaining = [row for row in ordered if after is None or position(row) > after]
    return page(remaining[: limit + 1], limit, position)
