"""An episode's free-text parts, such as research notes and the screenplay (project.episodes).

Each part is one row per episode in its own table, with its own revision, so editing one part
never conflicts with another.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sreditor.database import Database

# The revision of a part nobody has written yet; If-Match with it means "only if still empty".
UNWRITTEN = 0


class TextPart(StrEnum):
    """The table that holds each text part; never taken from input."""

    RESEARCH = "episode_research"
    SCREENPLAY = "episode_screenplays"
    SHORT_SCRIPT = "episode_short_scripts"  # the text is the script as JSON
    STYLE = "episode_styles"  # the text is the chosen library style's id, or empty
    CLASSIFICATION = "episode_classifications"  # the text is the classification as JSON


class StoredText(BaseModel):
    """A text part as stored: empty at revision 0 until someone writes it."""

    model_config = ConfigDict(frozen=True)

    text: str = ""
    revision: int = UNWRITTEN
    updated_at: datetime | None = None


class TextKey(BaseModel):
    """Which part of which episode."""

    model_config = ConfigDict(frozen=True)

    part: TextPart
    channel: str
    number: int


class PartChanged(ValueError):
    """A part of an episode, or a library entry, moved on from the revision the caller expected."""

    def __init__(self, what: str, current: int) -> None:
        super().__init__(what)
        self.current = current


class EpisodeTexts:
    """Reads and writes text parts; deleting the episode deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def read(self, key: TextKey) -> StoredText:
        """The part's text, empty at revision 0 when nobody has written it."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT text, revision, updated_at FROM {key.part}"
                " WHERE channel_slug = ? AND number = ?",
                (key.channel, key.number),
            ).fetchone()
        if row is None:
            return StoredText()
        return StoredText(
            text=row["text"],
            revision=row["revision"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def write(self, key: TextKey, text: str, expected_revision: int | None) -> int | None:
        """Replace the text; the new revision, or None when it was not at `expected_revision`."""
        values = (key.channel, key.number, text, self.database.now())
        if expected_revision is None:
            sql = (
                f"INSERT INTO {key.part} (channel_slug, number, text, revision, updated_at)"
                " VALUES (?, ?, ?, 1, ?) ON CONFLICT (channel_slug, number)"
                " DO UPDATE SET text = excluded.text, revision = revision + 1,"
                " updated_at = excluded.updated_at RETURNING revision"
            )
            parameters: tuple[object, ...] = values
        elif expected_revision == UNWRITTEN:
            sql = (
                f"INSERT OR IGNORE INTO {key.part} (channel_slug, number, text, revision,"
                " updated_at) VALUES (?, ?, ?, 1, ?) RETURNING revision"
            )
            parameters = values
        else:
            sql = (
                f"UPDATE {key.part} SET text = ?, revision = revision + 1, updated_at = ?"
                " WHERE channel_slug = ? AND number = ? AND revision = ? RETURNING revision"
            )
            parameters = (text, values[3], key.channel, key.number, expected_revision)
        with self.database.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        return None if row is None else int(row["revision"])
