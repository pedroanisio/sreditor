"""The full-text index over a channel's writing (migration 2; rule project.search).

`search_entries` holds one row per searchable thing, with the text to search, and `search_fts`
is an FTS5 index over those rows (accents and case ignored, like the title search). The rows are
a derived copy of text that other tables own; SQLite triggers keep the two in step, which is
what the normalization rule (code.data.normalized) asks of a copy.

Triggers keep the index fresh, rather than the stores writing to it, because the text changes
in many places (nine stores, cascades when an episode, pitch or channel goes, and the context
triggers) and a trigger cannot be forgotten by a new write path or skipped by a cascade. The
cost is a little work in each write transaction; the index is then never stale and never
half-written, since it commits or rolls back with the text itself.

Everything here is generated from one list of sources, so adding a source is one entry. The
migration is frozen once released: a later change to what is indexed is a new migration that
carries its own copy of what it needs.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sreditor.migrations import Migration, execute_script

SEARCH_VERSION = 2
ENTRIES_TABLE = "search_entries"
INDEX_TABLE = "search_fts"
# Folds accents (producao finds Produção) the same way the title search does.
TOKENIZER = "unicode61 remove_diacritics 2"
NEW = "NEW"
OLD = "OLD"
BACKFILL_ROW = "r"
LINE_BREAK = "char(10)"


class SearchKind(StrEnum):
    """What a search result is; the library kinds are the library entry's own kind."""

    EPISODE = "episode"
    RESEARCH = "research"
    SOURCE = "source"
    SCREENPLAY = "screenplay"
    SHORT_SCRIPT = "short_script"
    PITCH = "pitch"
    PITCH_NOTES = "pitch_notes"
    PITCH_SOURCE = "pitch_source"
    CHARACTER = "character"
    LOCATION = "location"
    PROP = "prop"
    STYLE = "style"
    EXTERNAL_MEDIA = "external_media"
    CONTEXT = "context"


class SearchSource(BaseModel):
    """One table whose rows are searchable and the SQL that reads an entry out of a row.

    Each expression names the row as `{row}` (NEW or OLD in a trigger, a table alias when the
    index is first built). `is_ref_global` marks references that are unique across channels, such
    as ids, so a delete can find the entry even when the row that named the channel is gone.
    """

    model_config = ConfigDict(frozen=True)

    table: str
    name: str
    kind: str
    channel: str
    ref: str
    episode: str
    pitch: str
    title: str
    body: str
    watched: tuple[str, ...]
    is_ref_global: bool = False


def episode_source(table: str, name: str, kind: SearchKind, body: str) -> SearchSource:
    """A part or the head of an episode, keyed by the episode's number in its channel."""
    return SearchSource(
        table=table,
        name=name,
        kind=f"'{kind}'",
        channel="{row}.channel_slug",
        ref="CAST({row}.number AS TEXT)",
        episode="{row}.number",
        pitch="NULL",
        title="''",
        body=body,
        watched=("text",),
    )


JSON_SPEECH = (
    "(SELECT group_concat(json_extract(value, '$.speech'), char(10))"
    " FROM json_each({row}.text, '$.blocks'))"
)
JSON_SCREEN_TEXTS = (
    "(SELECT group_concat(json_extract(value, '$.text'), char(10))"
    " FROM json_each({row}.text, '$.screen_texts'))"
)
# A script that is not valid JSON is indexed as it is, so a write never fails over the index.
SHORT_SCRIPT_BODY = (
    f"CASE WHEN json_valid({{row}}.text) THEN COALESCE({JSON_SPEECH}, '') || {LINE_BREAK}"
    f" || COALESCE({JSON_SCREEN_TEXTS}, '') ELSE {{row}}.text END"
)
CONTEXT_BODY = (
    "CASE WHEN json_valid({row}.answers) THEN COALESCE((SELECT group_concat(value, char(10))"
    " FROM json_each({row}.answers)), '') ELSE {row}.answers END"
)
PITCH_CHANNEL = "(SELECT channel_slug FROM pitches WHERE id = {row}.pitch_id)"


def sources() -> tuple[SearchSource, ...]:
    """Every table that feeds the index, in the order the index is first filled."""
    head = episode_source("episodes", "episodes", SearchKind.EPISODE, "{row}.summary")
    return (
        head.model_copy(update={"title": "{row}.title", "watched": ("title", "summary")}),
        episode_source("episode_research", "research", SearchKind.RESEARCH, "{row}.text"),
        episode_source("episode_screenplays", "screenplays", SearchKind.SCREENPLAY, "{row}.text"),
        episode_source(
            "episode_short_scripts", "short_scripts", SearchKind.SHORT_SCRIPT, SHORT_SCRIPT_BODY
        ),
        episode_source("episode_sources", "sources", SearchKind.SOURCE, "{row}.claim").model_copy(
            update={
                "ref": "{row}.id",
                "title": "{row}.title",
                "body": "{row}.claim || ' ' || {row}.url",
                "watched": ("title", "claim", "url"),
                "is_ref_global": True,
            }
        ),
        *pitch_sources(),
        library_source(),
        external_media_source(),
        context_source(),
    )


def pitch_sources() -> tuple[SearchSource, SearchSource, SearchSource]:
    """Pitches, their notes and their sources, keyed by the pitch's id."""
    pitch = SearchSource(
        table="pitches",
        name="pitches",
        kind=f"'{SearchKind.PITCH}'",
        channel="{row}.channel_slug",
        ref="{row}.id",
        episode="NULL",
        pitch="{row}.id",
        title="{row}.title",
        body="{row}.summary",
        watched=("title", "summary"),
        is_ref_global=True,
    )
    # Notes and sources carry the pitch's id, not its channel, so the channel is looked up.
    part = pitch.model_copy(update={"channel": PITCH_CHANNEL, "pitch": "{row}.pitch_id"})
    notes = part.model_copy(
        update={
            "table": "pitch_notes",
            "name": "pitch_notes",
            "kind": f"'{SearchKind.PITCH_NOTES}'",
            "ref": "{row}.pitch_id",
            "title": "''",
            "body": "{row}.text",
            "watched": ("text",),
        }
    )
    cited = part.model_copy(
        update={
            "table": "pitch_sources",
            "name": "pitch_sources",
            "kind": f"'{SearchKind.PITCH_SOURCE}'",
            "body": "{row}.claim || ' ' || {row}.url",
            "watched": ("title", "claim", "url"),
        }
    )
    return pitch, notes, cited


def library_source() -> SearchSource:
    """Characters, locations, props and styles; the entry's kind is the result's kind."""
    return SearchSource(
        table="channel_library",
        name="library",
        kind="{row}.kind",
        channel="{row}.channel_slug",
        ref="{row}.id",
        episode="NULL",
        pitch="NULL",
        title="{row}.name",
        body="{row}.description",
        watched=("name", "description"),
        is_ref_global=True,
    )


def external_media_source() -> SearchSource:
    """Third-party media: title, creator, credit line, license name and the rights note."""
    return SearchSource(
        table="external_media",
        name="external_media",
        kind=f"'{SearchKind.EXTERNAL_MEDIA}'",
        channel="{row}.channel_slug",
        ref="{row}.id",
        episode="NULL",
        pitch="NULL",
        title="{row}.title",
        body="{row}.creator || ' ' || {row}.attribution || ' ' || {row}.license_name"
        " || ' ' || {row}.rights_note",
        watched=("title", "creator", "attribution", "license_name", "rights_note"),
        is_ref_global=True,
    )


def context_source() -> SearchSource:
    """An item's what/who/where/when/why/how answers, tied to the episode or pitch they sit in."""
    return SearchSource(
        table="item_contexts",
        name="contexts",
        kind=f"'{SearchKind.CONTEXT}'",
        channel="{row}.channel_slug",
        ref="{row}.item_kind || ':' || {row}.item_id",
        episode="CASE {row}.item_kind WHEN 'episode' THEN CAST({row}.item_id AS INTEGER)"
        " WHEN 'source' THEN (SELECT number FROM episode_sources WHERE id = {row}.item_id) END",
        pitch="CASE {row}.item_kind WHEN 'pitch' THEN {row}.item_id WHEN 'pitch_source'"
        " THEN (SELECT pitch_id FROM pitch_sources WHERE id = {row}.item_id) END",
        title="''",
        body=CONTEXT_BODY,
        watched=("answers",),
    )


ENTRY_COLUMNS = "channel_slug, kind, ref, episode_number, pitch_id, title, body"

TABLES_SQL = f"""
CREATE TABLE {ENTRIES_TABLE} (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_slug TEXT NOT NULL REFERENCES channels (slug) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    episode_number INTEGER,
    pitch_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    UNIQUE (channel_slug, kind, ref)
);
CREATE INDEX search_entries_by_episode ON {ENTRIES_TABLE} (channel_slug, episode_number);
CREATE INDEX search_entries_by_pitch ON {ENTRIES_TABLE} (pitch_id);
CREATE VIRTUAL TABLE {INDEX_TABLE} USING fts5(
    title, body, content='{ENTRIES_TABLE}', content_rowid='position', tokenize='{TOKENIZER}'
);
CREATE TRIGGER search_entries_indexed AFTER INSERT ON {ENTRIES_TABLE} BEGIN
    INSERT INTO {INDEX_TABLE} (rowid, title, body) VALUES (NEW.position, NEW.title, NEW.body);
END;
CREATE TRIGGER search_entries_unindexed AFTER DELETE ON {ENTRIES_TABLE} BEGIN
    INSERT INTO {INDEX_TABLE} ({INDEX_TABLE}, rowid, title, body)
        VALUES ('delete', OLD.position, OLD.title, OLD.body);
END;
CREATE TRIGGER search_entries_reindexed AFTER UPDATE ON {ENTRIES_TABLE} BEGIN
    INSERT INTO {INDEX_TABLE} ({INDEX_TABLE}, rowid, title, body)
        VALUES ('delete', OLD.position, OLD.title, OLD.body);
    INSERT INTO {INDEX_TABLE} (rowid, title, body) VALUES (NEW.position, NEW.title, NEW.body);
END;
-- An episode or pitch that goes takes everything filed under it, whichever way its rows went.
CREATE TRIGGER search_episodes_forgotten AFTER DELETE ON episodes BEGIN
    DELETE FROM {ENTRIES_TABLE}
        WHERE channel_slug = OLD.channel_slug AND episode_number = OLD.number;
END;
CREATE TRIGGER search_pitches_forgotten AFTER DELETE ON pitches BEGIN
    DELETE FROM {ENTRIES_TABLE} WHERE pitch_id = OLD.id;
END;
"""


def values(source: SearchSource, row: str) -> str:
    """The seven entry columns for a row, as SQL expressions."""
    expressions = (
        source.channel,
        source.kind,
        source.ref,
        source.episode,
        source.pitch,
        source.title,
        source.body,
    )
    return ", ".join(expression.format(row=row) for expression in expressions)


def trigger_names(source: SearchSource) -> list[str]:
    """The names of a source's three triggers."""
    return [f"search_{source.name}_{suffix}" for suffix in ("added", "changed", "removed")]


def upsert_sql(source: SearchSource) -> str:
    """SQL that files or refiles the entry for the trigger's NEW row."""
    return (
        f"INSERT INTO {ENTRIES_TABLE} ({ENTRY_COLUMNS}) VALUES ({values(source, NEW)})"
        " ON CONFLICT (channel_slug, kind, ref) DO UPDATE SET episode_number = excluded.episode_number,"
        " pitch_id = excluded.pitch_id, title = excluded.title, body = excluded.body;"
    )


def forget_sql(source: SearchSource) -> str:
    """SQL that removes the entry of the trigger's OLD row."""
    kind = source.kind.format(row=OLD)
    ref = source.ref.format(row=OLD)
    channel = (
        "" if source.is_ref_global else f"channel_slug = {source.channel.format(row=OLD)} AND "
    )
    return f"DELETE FROM {ENTRIES_TABLE} WHERE {channel}kind = {kind} AND ref = {ref};"


def triggers_sql(source: SearchSource) -> str:
    """The insert, update and delete triggers that keep one source's entries current."""
    added, changed, removed = trigger_names(source)
    columns = ", ".join(source.watched)
    upsert = upsert_sql(source)
    return (
        f"CREATE TRIGGER {added} AFTER INSERT ON {source.table} BEGIN {upsert} END;\n"
        f"CREATE TRIGGER {changed} AFTER UPDATE OF {columns} ON {source.table} BEGIN {upsert} END;\n"
        f"CREATE TRIGGER {removed} AFTER DELETE ON {source.table} BEGIN {forget_sql(source)} END;\n"
    )


def backfill_sql(source: SearchSource) -> str:
    """SQL that files an entry for each row the source already holds.

    OR IGNORE skips a row whose parent is gone, which the foreign keys keep from happening.
    """
    return (
        f"INSERT OR IGNORE INTO {ENTRIES_TABLE} ({ENTRY_COLUMNS})"
        f" SELECT {values(source, BACKFILL_ROW)} FROM {source.table} AS {BACKFILL_ROW};"
    )


def drop_existing(connection: sqlite3.Connection) -> None:
    """Remove an earlier copy of the index, so the migration can run again over a restored one."""
    for source in sources():
        for name in trigger_names(source):
            connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    for table in (INDEX_TABLE, ENTRIES_TABLE):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for name in ("search_episodes_forgotten", "search_pitches_forgotten"):
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")


def build_search_index(connection: sqlite3.Connection) -> None:
    """Migration 2: create the index and its triggers, and file every row that already exists."""
    drop_existing(connection)
    execute_script(connection, TABLES_SQL)
    for source in sources():
        execute_script(connection, backfill_sql(source))
        execute_script(connection, triggers_sql(source))


SEARCH_INDEX = Migration(
    version=SEARCH_VERSION, name="full-text search index", apply=build_search_index
)
