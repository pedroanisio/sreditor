"""Finding things across the channels a person belongs to, for the workspace's quick search.

Titles are matched ignoring case and accents ("producao" finds "Produção"), which SQLite's
LIKE cannot do, so the store reads the titles of those channels and the matching happens here.
"""

from __future__ import annotations

import unicodedata
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sreditor.database import Database
from sreditor.field_types import ChannelSlug

# Enough titles for any channel set the switcher shows; the search reads no more than this.
SCAN_LIMIT = 5000
NUMBER_PREFIX = "#"


class HitKind(StrEnum):
    """What a search result is."""

    EPISODE = "episode"
    PITCH = "pitch"


class SearchHit(BaseModel):
    """An episode or a pitch whose title matched, with what its page address needs."""

    model_config = ConfigDict(frozen=True)

    kind: HitKind
    channel: ChannelSlug
    key: str  # the episode's number or the pitch's id
    title: str


def folded(text: str) -> str:
    """The text without accents and case, so that comparisons ignore both."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def matches(query: str, hit: SearchHit) -> bool:
    """Whether the hit's title holds the query, or the query names an episode's number."""
    wanted = folded(query.strip())
    if hit.kind is HitKind.EPISODE and wanted.removeprefix(NUMBER_PREFIX) == hit.key:
        return True
    return wanted in folded(hit.title)


class SearchStore:
    """Reads the titles of episodes and pitches in a set of channels."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def titles(self, channels: list[str]) -> list[SearchHit]:
        """Every episode and pitch title in these channels, newest channel order kept by slug."""
        if not channels:
            return []
        marks = ", ".join("?" for _ in channels)
        query = (
            f"SELECT 'episode' AS kind, channel_slug, CAST(number AS TEXT) AS key, title"
            f" FROM episodes WHERE channel_slug IN ({marks})"
            f" UNION ALL SELECT 'pitch', channel_slug, id, title"
            f" FROM pitches WHERE channel_slug IN ({marks})"
            f" ORDER BY channel_slug, kind, key LIMIT ?"
        )
        with self.database.connect() as connection:
            rows = connection.execute(query, (*channels, *channels, SCAN_LIMIT)).fetchall()
        return [
            SearchHit(
                kind=row["kind"], channel=row["channel_slug"], key=row["key"], title=row["title"]
            )
            for row in rows
        ]

    def find(self, channels: list[str], query: str, limit: int) -> list[SearchHit]:
        """Up to `limit` episodes and pitches in these channels whose title holds the query."""
        found = (hit for hit in self.titles(channels) if matches(query, hit))
        return [hit for _, hit in zip(range(limit), found, strict=False)]
