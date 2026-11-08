"""Channel search shared by the REST route, the MCP tool and the workspace (rule project.search).

Searching needs the read permission, so a member finds things only in their own channel and an
API token needs channels:read; a channel the caller cannot reach answers 404 before any query.
Each result says what it is, where it sits and which call opens it.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreditor.content_search import SearchPosition, SearchRow, match_expression
from sreditor.deps import ChannelAccess, Services
from sreditor.members import Permission
from sreditor.pagination import (
    DEFAULT_LIMIT,
    INVALID_CURSOR,
    MAX_LIMIT,
    Page,
    decode_cursor,
    encode_cursor,
)
from sreditor.problems import ProblemError
from sreditor.search_index import SearchKind

QUERY_MAX_LENGTH = 200
CONTEXT_SEPARATOR = ":"

SearchText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=QUERY_MAX_LENGTH)]
ArgumentValue = str | int | dict[str, str] | None


class SearchOpening(BaseModel):
    """The MCP tool and the API request that open a result's item in full."""

    model_config = ConfigDict(frozen=True)

    tool: str = Field(description="The MCP tool that reads the item.")
    arguments: dict[str, ArgumentValue] = Field(description="The arguments to call it with.")
    api_path: str = Field(description="The same read as a GET path under the API's base URL.")


class SearchResult(BaseModel):
    """One match: what it is, where it applies, a snippet and how to open it."""

    model_config = ConfigDict(frozen=True)

    kind: SearchKind = Field(description="What matched: an episode, its research, a pitch, ...")
    id: str = Field(description="The item's id; an episode's is its number.")
    episode: int | None = Field(description="The episode number it applies to, if any.")
    pitch_id: UUID | None = Field(description="The pitch it applies to, if any.")
    title: str = Field(description="The title of the item, or of the episode or pitch it is in.")
    snippet: str = Field(description="Text around the match, with the match between « and ».")
    open_with: SearchOpening


class ContentQuery(BaseModel):
    """What to look for in a channel, and which page of the results to read."""

    model_config = ConfigDict(frozen=True)

    text: SearchText
    cursor: str | None = None
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


def episode_opening(slug: str, row: SearchRow, tool: str, suffix: str) -> SearchOpening:
    """How to open something an episode holds: a tool taking the channel and the number."""
    number = row.episode_number
    return SearchOpening(
        tool=tool,
        arguments={"slug": slug, "number": number},
        api_path=f"/channels/{slug}/episodes/{number}{suffix}",
    )


def pitch_opening(slug: str, row: SearchRow) -> SearchOpening:
    """How to open a pitch, its notes or one of its sources."""
    return SearchOpening(
        tool="get_pitch",
        arguments={"slug": slug, "pitch_id": str(row.pitch_id)},
        api_path=f"/channels/{slug}/pitches/{row.pitch_id}",
    )


def library_opening(slug: str, row: SearchRow) -> SearchOpening:
    """How to open a library entry; its kind names the collection."""
    return SearchOpening(
        tool="get_library_sheet",
        arguments={"slug": slug, "kind": row.kind.value, "entry_id": row.ref},
        api_path=f"/channels/{slug}/{row.kind.value}s/{row.ref}",
    )


def external_media_opening(slug: str, row: SearchRow) -> SearchOpening:
    """How to open a third-party item: the list tool, since the item has no tool of its own."""
    return SearchOpening(
        tool="list_external_media",
        arguments={"slug": slug},
        api_path=f"/channels/{slug}/external-media/{row.ref}",
    )


def context_opening(slug: str, row: SearchRow) -> SearchOpening:
    """How to open an item's context; the entry's id holds the item's kind and id."""
    item_kind, _, item_id = row.ref.partition(CONTEXT_SEPARATOR)
    return SearchOpening(
        tool="get_context",
        arguments={"slug": slug, "item": {"kind": item_kind, "id": item_id}},
        api_path=f"/channels/{slug}/items/{item_kind}/{item_id}/context",
    )


def opening_for(slug: str, row: SearchRow) -> SearchOpening:
    """The call that opens a result, by what it is."""
    kind = row.kind
    if kind is SearchKind.EPISODE:
        return episode_opening(slug, row, "get_episode", "")
    if kind in (SearchKind.RESEARCH, SearchKind.SOURCE):
        return episode_opening(slug, row, "get_research", "/research")
    if kind is SearchKind.SCREENPLAY:
        return episode_opening(slug, row, "get_screenplay", "/screenplay")
    if kind is SearchKind.SHORT_SCRIPT:
        return episode_opening(slug, row, "get_short_script", "/short-script")
    if kind in (SearchKind.PITCH, SearchKind.PITCH_NOTES, SearchKind.PITCH_SOURCE):
        return pitch_opening(slug, row)
    if kind is SearchKind.EXTERNAL_MEDIA:
        return external_media_opening(slug, row)
    if kind is SearchKind.CONTEXT:
        return context_opening(slug, row)
    return library_opening(slug, row)


def result_of(slug: str, row: SearchRow) -> SearchResult:
    """A stored match as the caller sees it."""
    return SearchResult(
        kind=row.kind,
        id=item_id(row),
        episode=row.episode_number,
        pitch_id=row.pitch_id,
        title=row.title,
        snippet=row.snippet,
        open_with=opening_for(slug, row),
    )


def item_id(row: SearchRow) -> str:
    """The id of the matched item: a pitch's notes are the pitch's, a source has its own."""
    return str(row.pitch_id) if row.kind is SearchKind.PITCH_NOTES else row.ref


def position_of(cursor: str | None) -> SearchPosition | None:
    """The position a cursor names; a cursor this module did not make is a 400 problem."""
    decoded = decode_cursor(cursor)
    if decoded is None:
        return None
    try:
        score, position = json.loads(decoded)
        return SearchPosition(score=score, position=position)
    except (ValueError, TypeError) as invalid:
        raise ProblemError(HTTPStatus.BAD_REQUEST, INVALID_CURSOR, "invalid-cursor") from invalid


def cursor_after(row: SearchRow) -> str:
    """The cursor that continues after this result."""
    return encode_cursor(json.dumps([row.score, row.position]))


def search_channel(app: Services, access: ChannelAccess, query: ContentQuery) -> Page[SearchResult]:
    """Matches for the text in the channel, best first; words only, never FTS5 syntax."""
    access.require(Permission.READ)
    match = match_expression(query.text)
    if match is None:
        return Page[SearchResult](items=[])
    slug = access.channel.slug
    rows = app.content_search.search(slug, match, position_of(query.cursor), query.limit + 1)
    shown = rows[: query.limit]
    more = len(rows) > query.limit
    return Page[SearchResult](
        items=[result_of(slug, row) for row in shown],
        next_cursor=cursor_after(shown[-1]) if more else None,
    )
