"""MCP tool to record a review of an episode (rules agentic.mcp, project.reviews).

Reading reviews needs no tool of its own: get_episode returns how many reviews an episode has and
the latest one, and the API lists them all. Reviews are append-only records, so the only tool
adds one; deleting a review is a person's or an owner's act on the pages or the API.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from sreditor import review_operations as reviews
from sreditor.deps import Services
from sreditor.mcp_idempotency import IdempotencyKey, IdempotentCall, run_once
from sreditor.mcp_server import Effect, reported, tool_annotations
from sreditor.pitch_tools import channel_access
from sreditor.reviews import Review, ReviewInput


def review_error_message(error: Exception) -> str | None:
    """How to fix a refused review (agentic.mcp.errors); None for other errors."""
    if isinstance(error, reviews.ReviewRefused):
        where = ".".join(str(part) for part in error.location)
        return f"{where}: {error}"
    return None


def review_reported[T](action: Callable[[], T]) -> T:
    """Run a review tool's action; a refusal names its field and fix, others go to `reported`."""

    def guarded() -> T:
        try:
            return action()
        except reviews.ReviewRefused as refusal:
            raise ToolError(review_error_message(refusal) or str(refusal)) from refusal

    return reported(guarded)


def add_review_tools(server: MCPServer, services: Services) -> None:
    """The tool that records a review."""

    @server.tool(annotations=tool_annotations("Add a review", Effect.CREATE))
    def add_review(
        context: Context,
        slug: str,
        number: int,
        review: ReviewInput,
        idempotency_key: IdempotencyKey = None,
    ) -> Annotated[CallToolResult, Review]:
        """Record a review of an episode: your `verdict` (approved, changes_requested or
        rejected), `notes` in Markdown and, when you watched a file, its `media_id` (a render or
        a cut's final video from list_media) with the time ranges you `seen` as start_s and
        end_s in seconds. Reviews are append-only: you cannot edit one, so add a new one when
        you change your mind. The latest review decides the verdict get_episode shows, and an
        episode moved to published without an approving one only warns. Needs the owner or
        editor role and editorial:write; the review names you and your token. Each call adds
        another review unless you pass an `idempotency_key`, any unique string per intended
        review: a retry with the same key and review returns the review the first call made."""
        call = IdempotentCall(
            operation=f"add_review/{slug}/{number}", arguments=review, key=idempotency_key
        )

        def add() -> CallToolResult:
            channel = channel_access(services, context, slug)
            return run_once(
                services,
                context,
                call,
                lambda: reviews.add_review(services, channel, number, review),
                Review,
            )

        return review_reported(add)
