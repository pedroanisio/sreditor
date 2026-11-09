"""The project's rules as CSV: the text form git keeps, and loading it into a stmt store."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.store import Statement, StmtClient, StoreSettings

RULES_CSV = "store/rules.csv"
ROOT_SCOPE = "/"
# Cells that hold a list or an object are JSON text; an empty cell is the default.
JSON_COLUMNS = ("conditionals", "rule", "requires", "evidence")
COLUMNS = (
    "id",
    "category",
    "priority",
    "owner_type",
    "owner_id",
    "statement",
    "override_key",
    "ttl_seconds",
    *JSON_COLUMNS,
)


class RulesFileError(ValueError):
    """The rules CSV cannot be read: wrong columns or a cell that is not valid."""


class LoadReport(BaseModel):
    """What loading the CSV changed in the store, by statement id."""

    model_config = ConfigDict(frozen=True)

    created: list[str]
    revised: list[str]
    disabled: list[str]

    def summary(self) -> str:
        """One line for the command line."""
        return (
            f"rules: {len(self.created)} created, {len(self.revised)} revised, "
            f"{len(self.disabled)} disabled"
        )


def optional_text(value: object) -> str:
    """A cell for a value that may be missing: empty when it is."""
    return "" if value is None else str(value)


def row_cells(statement: Statement) -> dict[str, str]:
    """One statement as CSV cells."""
    document = statement.document()
    cells = {
        "id": statement.id,
        "category": statement.category,
        "priority": str(statement.priority),
        "owner_type": statement.owner.type,
        "owner_id": statement.owner.id,
        "statement": statement.statement,
        "override_key": optional_text(statement.override_key),
        "ttl_seconds": optional_text(statement.ttl_seconds),
    }
    for column in JSON_COLUMNS:
        value = document.get(column)
        cells[column] = json.dumps(value, ensure_ascii=False, sort_keys=True) if value else ""
    return cells


def render_rules_csv(statements: Sequence[Statement], settings: StoreSettings) -> str:
    """store/rules.csv: the root scope's statements sorted by id, so a change diffs as one line."""
    del settings  # the CSV names no store: it loads into any
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for statement in sorted(statements, key=lambda statement: statement.id):
        if statement.scope == ROOT_SCOPE:
            writer.writerow(row_cells(statement))
    return buffer.getvalue()


def statement_from_cells(cells: dict[str, str]) -> Statement:
    """The statement one CSV row describes, as a new root-scope revision."""
    data: dict[str, Any] = {
        "id": cells["id"],
        "revision": 0,
        "scope": ROOT_SCOPE,
        "statement": cells["statement"],
        "owner": {"type": cells["owner_type"], "id": cells["owner_id"]},
        "category": cells["category"],
        "priority": int(cells["priority"]),
        "override_key": cells["override_key"] or None,
        "ttl_seconds": int(cells["ttl_seconds"]) if cells["ttl_seconds"] else None,
    }
    for column in JSON_COLUMNS:
        if cells[column]:
            data[column] = json.loads(cells[column])
    return Statement.model_validate(data)


def parse_rules_csv(text: str) -> list[Statement]:
    """Read store/rules.csv; refuse other columns or a cell that is not valid."""
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != COLUMNS:
        raise RulesFileError(f"rules CSV columns must be {', '.join(COLUMNS)}")
    statements: list[Statement] = []
    for cells in reader:
        try:
            statements.append(statement_from_cells(cells))
        except (ValueError, ValidationError) as invalid:
            raise RulesFileError(f"rules CSV line {reader.line_num}: {invalid}") from invalid
    return statements


def load_rules(client: StmtClient, wanted: Sequence[Statement]) -> LoadReport:
    """Make the store's root scope match the CSV, leaving every other scope as it is.

    Statements missing from the CSV are disabled rather than lost: the store keeps their history.
    """
    client.init()
    current = {head.id: head for head in client.statements() if head.scope == ROOT_SCOPE}
    created: list[str] = []
    revised: list[str] = []
    for statement in wanted:
        head = current.get(statement.id) or client.get(statement.id, ROOT_SCOPE)
        if head is None:
            client.write(statement.id, statement.document(), ROOT_SCOPE)
            created.append(statement.id)
        elif head.document() != statement.document():
            client.write(statement.id, statement.document(), ROOT_SCOPE, head.revision)
            revised.append(statement.id)
    disabled = sorted(set(current) - {statement.id for statement in wanted})
    for statement_id in disabled:
        head = current[statement_id]
        client.write(statement_id, {**head.document(), "enabled": False}, ROOT_SCOPE, head.revision)
    return LoadReport(created=created, revised=revised, disabled=disabled)
