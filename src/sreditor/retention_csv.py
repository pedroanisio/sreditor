"""Reading a platform's retention export: a CSV of the second and the percent still watching.

The first row is a header naming exactly two columns, as "second,percent" or the names Instagram
and YouTube exports use ("Time (s)", "Audience retention (%)"); a second may also be a clock such
as 1:05 and a percent may carry a % sign or a decimal comma. Anything else is refused, with a
problem naming the row and field at fault (rule project.metrics).
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.i18n import N_
from sreditor.post_metrics import MAX_RETENTION_POINTS, RetentionPoint, in_order

MAX_CSV_CHARS = 200_000
MAX_REPORTED_PROBLEMS = 20
SECONDS_PER_MINUTE = 60
SECOND_FIELD = "second"
PERCENT_FIELD = "percent"
SECOND_WORDS = frozenset(
    {"second", "seconds", "sec", "secs", "segundo", "segundos", "time", "tempo"}
)
PERCENT_WORDS = frozenset(
    {"percent", "percentage", "retention", "retained", "watching", "audience", "retencao"}
)
PERCENT_SIGN = "%"
NOT_WORD = re.compile(r"[^a-z0-9]+")
CLOCK_SEPARATOR = ":"
SEMICOLON = ";"
COMMA = ","
DOT = "."
BYTE_ORDER_MARK = "﻿"
FILE_FIELD = "csv"
HEADER_ROW = 1
FIRST_BODY_ROW = 2


class RetentionProblemKind(StrEnum):
    """What can be wrong with a retention CSV."""

    HEADER = "header"
    EMPTY = "empty"
    TOO_LARGE = "too_large"
    ROWS = "rows"
    NOT_A_NUMBER = "not_a_number"
    OUT_OF_RANGE = "out_of_range"
    REPEATED = "repeated"


PROBLEM_MESSAGES: dict[RetentionProblemKind, str] = {
    RetentionProblemKind.HEADER: N_(
        "The first row must name two columns: the second and the percent still watching, such as"
        " second,percent."
    ),
    RetentionProblemKind.EMPTY: N_("The CSV is empty."),
    RetentionProblemKind.TOO_LARGE: N_("The CSV is larger than %(limit)s characters."),
    RetentionProblemKind.ROWS: N_("The CSV needs between 1 and %(limit)s rows below its header."),
    RetentionProblemKind.NOT_A_NUMBER: N_("Row %(row)s: %(field)s must be a number."),
    RetentionProblemKind.OUT_OF_RANGE: N_("Row %(row)s: %(field)s is out of range."),
    RetentionProblemKind.REPEATED: N_("Each second may appear only once."),
}


class CsvProblem(BaseModel):
    """What is wrong in one place of a retention CSV, with the values its message names."""

    model_config = ConfigDict(frozen=True)

    kind: RetentionProblemKind
    row: int
    field: str
    message: str
    values: dict[str, str]


def problem(
    kind: RetentionProblemKind, row: int = 0, field: str = FILE_FIELD, **values: object
) -> CsvProblem:
    """A problem with its English message filled in."""
    shown = {"row": str(row), "field": field, **{name: str(item) for name, item in values.items()}}
    return CsvProblem(
        kind=kind, row=row, field=field, message=PROBLEM_MESSAGES[kind] % shown, values=shown
    )


class RetentionCsvError(ValueError):
    """The CSV is not a retention export; `problems` names each field at fault."""

    def __init__(self, problems: list[CsvProblem]) -> None:
        super().__init__(problems[0].message)
        self.problems = problems


def refused(
    kind: RetentionProblemKind, row: int = 0, field: str = FILE_FIELD, **values: object
) -> RetentionCsvError:
    """The error for one problem."""
    return RetentionCsvError([problem(kind, row, field, **values)])


def header_words(name: str) -> set[str]:
    """The words of a column name, lowercase and without punctuation."""
    return set(NOT_WORD.sub(" ", name.casefold()).split())


def column_roles(header: list[str]) -> tuple[int, int]:
    """The positions of the second and percent columns; RetentionCsvError when not exactly those."""
    names = [name.strip() for name in header]
    while names and not names[-1]:
        names.pop()
    seconds = [i for i, name in enumerate(names) if header_words(name) & SECOND_WORDS]
    percents = [
        i
        for i, name in enumerate(names)
        if header_words(name) & PERCENT_WORDS or PERCENT_SIGN in name
    ]
    is_pair = len(names) == len((SECOND_FIELD, PERCENT_FIELD))
    if not is_pair or len(seconds) != 1 or len(percents) != 1 or seconds == percents:
        raise refused(RetentionProblemKind.HEADER, HEADER_ROW, "header")
    return seconds[0], percents[0]


def seconds_of(raw: str) -> float:
    """A second as 12, 12.5 or a clock such as 1:05; ValueError when it is none of these."""
    total = 0.0
    for part in raw.strip().split(CLOCK_SEPARATOR):
        total = total * SECONDS_PER_MINUTE + float(part.replace(COMMA, DOT))
    return total


def percent_of(raw: str) -> float:
    """A percent as 82, 82.5, 82,5 or 82%; ValueError when it is none of these."""
    cleaned = raw.strip().removesuffix(PERCENT_SIGN).strip()
    return float(cleaned if DOT in cleaned else cleaned.replace(COMMA, DOT))


def read_cell(
    cells: list[str], position: int, field: str, row: int, convert: Callable[[str], float]
) -> float:
    """One cell as a number; RetentionCsvError naming the field when it is not one."""
    try:
        return convert(cells[position])
    except (ValueError, IndexError) as unreadable:
        raise refused(RetentionProblemKind.NOT_A_NUMBER, row, field) from unreadable


def point_of(cells: list[str], roles: tuple[int, int], row: int) -> RetentionPoint:
    """One row as a point; RetentionCsvError naming the field at fault."""
    second = read_cell(cells, roles[0], SECOND_FIELD, row, seconds_of)
    percent = read_cell(cells, roles[1], PERCENT_FIELD, row, percent_of)
    try:
        return RetentionPoint(second=second, percent=percent)
    except ValidationError as invalid:
        raise refused(
            RetentionProblemKind.OUT_OF_RANGE, row, str(invalid.errors()[0]["loc"][0])
        ) from invalid


def csv_rows(text: str) -> list[list[str]]:
    """The non-empty rows of the CSV, split on the delimiter its header uses."""
    lines = [line for line in text.lstrip(BYTE_ORDER_MARK).splitlines() if line.strip()]
    if not lines:
        raise refused(RetentionProblemKind.EMPTY)
    uses_semicolon = SEMICOLON in lines[0] and COMMA not in lines[0]
    return list(csv.reader(lines, delimiter=SEMICOLON if uses_semicolon else COMMA))


def points_of(body: list[list[str]], roles: tuple[int, int]) -> list[RetentionPoint]:
    """Every row as a point; RetentionCsvError listing the rows at fault."""
    points: list[RetentionPoint] = []
    problems: list[CsvProblem] = []
    for row, cells in enumerate(body, start=FIRST_BODY_ROW):
        try:
            points.append(point_of(cells, roles, row))
        except RetentionCsvError as unreadable:
            problems.extend(unreadable.problems)
    if problems:
        raise RetentionCsvError(problems[:MAX_REPORTED_PROBLEMS])
    return points


def parse_retention_csv(text: str) -> list[RetentionPoint]:
    """Retention per second from a two-column CSV with a header; RetentionCsvError otherwise."""
    if len(text) > MAX_CSV_CHARS:
        raise refused(RetentionProblemKind.TOO_LARGE, limit=MAX_CSV_CHARS)
    rows = csv_rows(text)
    roles = column_roles(rows[0])
    if not rows[1:] or len(rows) - 1 > MAX_RETENTION_POINTS:
        raise refused(RetentionProblemKind.ROWS, limit=MAX_RETENTION_POINTS)
    points = points_of(rows[1:], roles)
    try:
        return in_order(points)
    except ValueError as repeated:
        raise refused(RetentionProblemKind.REPEATED, field=SECOND_FIELD) from repeated
