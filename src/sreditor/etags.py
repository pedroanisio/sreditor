"""ETags and If-Match for replaceable resources (rule agentic.concurrency.etag)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Header, status

from sreditor.problems import ProblemError

ANY_REVISION = "*"
WEAK_PREFIX = "W/"

IfMatch = Annotated[
    str | None,
    Header(
        alias="If-Match",
        description="The ETag you last read; the change is refused with 412 if it moved on.",
    ),
]


def etag(revision: int) -> str:
    """The ETag of a resource at a revision."""
    return f'"{revision}"'


def expected_revision(if_match: str | None) -> int | None:
    """The revision an If-Match header names; None when absent or `*`."""
    if if_match is None or if_match.strip() == ANY_REVISION:
        return None
    value = if_match.strip().removeprefix(WEAK_PREFIX).strip('"')
    if not value.isdigit():
        raise ProblemError(
            status.HTTP_412_PRECONDITION_FAILED, "If-Match must be an ETag from this API"
        )
    return int(value)
