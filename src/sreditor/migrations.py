"""Numbered schema migrations with a stored version (rule code.data.migrations).

The database records each migration it applied in `schema_version`; its version is the highest.
A migration is a numbered function that changes the schema or the rows, runs once, inside one
transaction together with the row that records it, and so either happens completely or not at
all: a failing migration leaves the database and its version as they were.

Migrations run when the application opens the database. `BEGIN IMMEDIATE` takes the write lock
first and the version is read again under it, so two processes opening one database never apply
the same migration twice. A database written by a newer application is refused, because older
code cannot know what a newer schema holds.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

LOGGER = logging.getLogger("sreditor.migrations")

VERSION_TABLE = "schema_version"
FIRST_VERSION = 1
NO_VERSION = 0

VERSION_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class MigrationError(RuntimeError):
    """The database cannot be brought to the schema this version of the application needs."""


class Migration(BaseModel):
    """One numbered schema change; `apply` runs inside a transaction the runner owns."""

    model_config = ConfigDict(frozen=True)

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def check_numbering(migrations: Sequence[Migration]) -> None:
    """Refuse a list whose versions are not 1, 2, 3 and so on, in order."""
    expected = range(FIRST_VERSION, FIRST_VERSION + len(migrations))
    if [migration.version for migration in migrations] != list(expected):
        raise ValueError("Migrations must be numbered 1, 2, 3, ... in order, without gaps")


def current_version(connection: sqlite3.Connection) -> int:
    """The highest migration applied; 0 for a database without a version table or with no rows."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (VERSION_TABLE,)
    ).fetchone()
    if exists is None:
        return NO_VERSION
    row = connection.execute(f"SELECT MAX(version) FROM {VERSION_TABLE}").fetchone()
    return int(row[0]) if row[0] is not None else NO_VERSION


def execute_script(connection: sqlite3.Connection, script: str) -> None:
    """Run SQL statements one by one inside the open transaction.

    `executescript` commits first, which would break a migration's all-or-nothing guarantee;
    `complete_statement` keeps the semicolons inside a trigger's body from splitting it.
    """
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""


def apply_one(connection: sqlite3.Connection, migration: Migration, applied_at: str) -> None:
    """Apply one migration and record it in a single transaction, unless another process did."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(VERSION_TABLE_SQL)
        if current_version(connection) < migration.version:
            migration.apply(connection)
            connection.execute(
                f"INSERT INTO {VERSION_TABLE} (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, applied_at),
            )
            LOGGER.info("Applied schema migration %d: %s", migration.version, migration.name)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def run_migrations(
    connection: sqlite3.Connection, migrations: Sequence[Migration], applied_at: str
) -> int:
    """Bring the database to the newest migration and return its version.

    The connection must be in autocommit mode (`isolation_level=None`), so each migration's
    transaction is the one this module opens.
    """
    check_numbering(migrations)
    latest = migrations[-1].version
    found = current_version(connection)
    if found > latest:
        raise MigrationError(
            f"The database is at schema version {found}, newer than the {latest} this version of "
            "sreditor knows; run the version of sreditor that wrote it"
        )
    for migration in migrations[found:]:
        apply_one(connection, migration, applied_at)
    return latest
