"""The channel search route: where did we write about X (rule project.search)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from sreditor import search_operations as searching
from sreditor.deps import Access, ServicesDep
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.search_operations import QUERY_MAX_LENGTH, ContentQuery, SearchResult

router = APIRouter(tags=["search"])

SearchWords = Annotated[
    str,
    Query(
        min_length=1,
        max_length=QUERY_MAX_LENGTH,
        description="Words to look for; every word must appear, and the last may be a prefix. "
        "Quotes, AND, OR, NEAR, * and parentheses are ordinary text, never search syntax.",
    ),
]


@router.get("/channels/{slug}/search", summary="Search a channel's writing")
def search_channel(
    access: Access,
    app: ServicesDep,
    q: SearchWords,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_LIMIT,
) -> Page[SearchResult]:
    """Find where the channel wrote about something: episode titles and summaries, research
    notes, sources, screenplays and short scripts, pitches with their notes and sources, library
    characters, locations, props and styles, third-party media and context answers.

    Use it when you do not know which episode or pitch holds a text; to list episodes or pitches
    by status or number, use their list calls instead. Accents and case are ignored. Results
    are best first, each with its kind, id, the episode number or pitch it applies to, a snippet
    with the match between « and », and `open_with`: the MCP tool and API path that read the
    item in full. Needs channels:read; other channels answer 404. Pass `next_cursor` as `cursor`
    for more.
    """
    return searching.search_channel(app, access, ContentQuery(text=q, cursor=cursor, limit=limit))
