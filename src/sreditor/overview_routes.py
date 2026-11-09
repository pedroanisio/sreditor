"""Overview routes: an episode in one call, and which episodes of a channel need work (project.readiness)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from sreditor import overview_operations
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number
from sreditor.overview_operations import EpisodeOverview, EpisodeReadiness, Section
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page

router = APIRouter(tags=["episodes"])

Include = Annotated[
    list[Section] | None,
    Query(
        description="Repeat the parameter to name the sections to include; omit it for the "
        "default set (research, screenplay, short_script, style, media, claims, readiness)."
    ),
]


@router.get("/channels/{slug}/episodes/{number}/overview", summary="Read an episode in one call")
def get_episode_overview(
    number: Number, access: Access, app: ServicesDep, include: Include = None
) -> EpisodeOverview:
    """Read an episode's details and, per section, a compact digest with the revision to send
    back when writing that part, in one call instead of one per part. `readiness` lists what
    still holds the episode back: each item has a severity (blocker stops an srstudio export and
    a render, warning only warns), a message and the `fix_tool` (an MCP tool name) that
    resolves it. Digests hold counts and short excerpts; each names the call that reads the
    whole part. Waivers are never included. Use `include` to ask for pitches, credits, context
    or delivery, or to leave sections out.
    """
    return overview_operations.episode_overview(
        app, access, number, frozenset(include) if include else None
    )


@router.get("/channels/{slug}/readiness", summary="Find the episodes that need work")
def get_channel_readiness(
    access: Access, app: ServicesDep, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT
) -> Page[EpisodeReadiness]:
    """List the channel's episodes by number, each with its status and how many blockers and
    warnings its readiness has, to find which episode needs work. Read one episode's gaps with
    get_episode_overview. Counts cover only the areas the caller may read.
    """
    return overview_operations.list_readiness(app, access, cursor, limit)
