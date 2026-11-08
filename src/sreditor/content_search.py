"""Full-text search over one channel's writing: the store behind search_channel (rule project.search).

The index itself, and the triggers that keep it current, live in search_index.py. This module
turns what a person typed into a safe FTS5 expression and reads ranked rows back, one channel at
a time: a channel's slug is part of every query, so a result never comes from another channel.
"""

from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.database import Database
from sreditor.search_index import ENTRIES_TABLE, INDEX_TABLE, SearchKind

# Words are runs of letters, digits and underscores in any script, so quotes, operators,
# parentheses, colons and stars in what a person typed are never passed to FTS5.
WORD = re.compile(r"\w+")
MAX_WORDS = 12
MATCH_OPEN = "«"
MATCH_CLOSE = "»"
ELLIPSIS = "…"
SNIPPET_WORDS = 14
# A hit in a title counts for more than one in the body.
TITLE_WEIGHT = 4.0
BODY_WEIGHT = 1.0
WHOLE_ENTRY = -1


def match_expression(text: str) -> str | None:
    """An FTS5 MATCH expression for the text, or None when it holds no words.

    Each word becomes a quoted prefix term, so "mond" finds Mondays, and the terms are ANDed.
    Quoting makes AND, OR, NOT and NEAR plain words; the words themselves cannot hold a quote.
    """
    words = WORD.findall(text)[:MAX_WORDS]
    return " ".join(f'"{word}"*' for word in words) if words else None


class SearchPosition(BaseModel):
    """Where a page ended: the last result's score and entry, to continue after it."""

    model_config = ConfigDict(frozen=True)

    score: float
    position: int


class SearchRow(BaseModel):
    """One match as stored: what it is, where it sits, the title it falls under and a snippet."""

    model_config = ConfigDict(frozen=True)

    position: int
    score: float
    kind: SearchKind
    ref: str
    episode_number: int | None
    pitch_id: UUID | None
    title: str
    snippet: str


SEARCH_SQL = f"""
WITH hits AS MATERIALIZED (
    SELECT e.position AS position, bm25({INDEX_TABLE}, {TITLE_WEIGHT}, {BODY_WEIGHT}) AS score,
        e.kind AS kind, e.ref AS ref, e.episode_number AS episode_number, e.pitch_id AS pitch_id,
        COALESCE(NULLIF(e.title, ''), episode.title, pitch.title, '') AS title,
        snippet({INDEX_TABLE}, {WHOLE_ENTRY}, ?, ?, ?, {SNIPPET_WORDS}) AS snippet
    FROM {INDEX_TABLE}
    JOIN {ENTRIES_TABLE} AS e ON e.position = {INDEX_TABLE}.rowid
    LEFT JOIN episodes AS episode
        ON episode.channel_slug = e.channel_slug AND episode.number = e.episode_number
    LEFT JOIN pitches AS pitch ON pitch.id = e.pitch_id
    WHERE {INDEX_TABLE} MATCH ? AND e.channel_slug = ?
)
SELECT * FROM hits
WHERE ? IS NULL OR score > ? OR (score = ? AND position > ?)
ORDER BY score, position
LIMIT ?
"""


class ContentSearchStore:
    """Reads ranked matches from the full-text index, within one channel."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def search(
        self, channel: str, match: str, after: SearchPosition | None, limit: int
    ) -> list[SearchRow]:
        """Up to `limit` matches in the channel, best first, after the position if one is given.

        `match` must come from match_expression. Ties in score keep the order the entries were
        filed in, so a cursor never skips or repeats a result while nothing changes.
        """
        score = after.score if after else None
        position = after.position if after else None
        parameters = (
            MATCH_OPEN,
            MATCH_CLOSE,
            ELLIPSIS,
            match,
            channel,
            score,
            score,
            score,
            position,
            limit,
        )
        with self.database.connect() as connection:
            rows = connection.execute(SEARCH_SQL, parameters).fetchall()
        return [SearchRow.model_validate(dict(row)) for row in rows]
