"""Reading a retention CSV exported by a platform or typed by hand (rule project.metrics)."""

from __future__ import annotations

import pytest

from sreditor.post_metrics import MAX_RETENTION_POINTS, RetentionPoint
from sreditor.retention_csv import (
    MAX_CSV_CHARS,
    ProblemKind,
    RetentionCsvError,
    parse_retention_csv,
)


def points(text: str) -> list[tuple[float, float]]:
    return [(point.second, point.percent) for point in parse_retention_csv(text)]


def problem_of(text: str) -> ProblemKind:
    with pytest.raises(RetentionCsvError) as refused:
        parse_retention_csv(text)
    return refused.value.problems[0].kind


def test_parse_plain_two_column_csv_returns_points() -> None:
    assert points("second,percent\n0,100\n1,92.5\n2,80\n") == [(0, 100), (1, 92.5), (2, 80)]


def test_parse_returns_retention_point_models() -> None:
    assert parse_retention_csv("second,percent\n0,100") == [RetentionPoint(second=0, percent=100)]


def test_parse_two_percent_columns_is_refused_as_bad_header() -> None:
    text = "Video position (%),Absolute audience retention (%)\n"
    assert problem_of(text + "0,100") is ProblemKind.HEADER


def test_parse_instagram_style_header_with_time_and_retention() -> None:
    assert points("Time (s),Audience retention (%)\n0,100%\n1,90%\n") == [(0, 100), (1, 90)]


def test_parse_portuguese_header_with_decimal_comma_and_semicolons() -> None:
    assert points('Segundo;Retenção (%)\n0;100\n1;"87,5"\n') == [(0, 100), (1, 87.5)]


def test_parse_clock_seconds_reads_minutes_and_seconds() -> None:
    assert points("time,percent\n0:00,100\n1:05,40\n") == [(0, 100), (65, 40)]


def test_parse_columns_in_either_order() -> None:
    assert points("percent,second\n100,0\n80,1\n") == [(0, 100), (1, 80)]


def test_parse_byte_order_mark_and_blank_lines_are_ignored() -> None:
    assert points("﻿second,percent\n\n0,100\n\n") == [(0, 100)]


def test_parse_unordered_rows_are_sorted_by_second() -> None:
    assert [second for second, _ in points("second,percent\n2,50\n0,100\n")] == [0, 2]


def test_parse_empty_text_is_refused() -> None:
    assert problem_of("  \n") is ProblemKind.EMPTY


def test_parse_header_only_is_refused_as_no_rows() -> None:
    assert problem_of("second,percent\n") is ProblemKind.ROWS


def test_parse_three_columns_is_refused_as_bad_header() -> None:
    assert problem_of("second,percent,views\n0,100,5") is ProblemKind.HEADER


def test_parse_header_without_percent_is_refused() -> None:
    assert problem_of("second,likes\n0,5") is ProblemKind.HEADER


def test_parse_non_numeric_cell_names_the_row_and_field() -> None:
    with pytest.raises(RetentionCsvError) as refused:
        parse_retention_csv("second,percent\n0,100\n1,lots\n")
    found = refused.value.problems[0]
    assert (found.row, found.field, found.kind) == (3, "percent", ProblemKind.NOT_A_NUMBER)


def test_parse_missing_cell_is_not_a_number() -> None:
    assert problem_of("second,percent\n0\n") is ProblemKind.NOT_A_NUMBER


def test_parse_negative_second_is_out_of_range() -> None:
    assert problem_of("second,percent\n-1,100\n") is ProblemKind.OUT_OF_RANGE


def test_parse_percent_over_limit_is_out_of_range() -> None:
    assert problem_of("second,percent\n0,100000\n") is ProblemKind.OUT_OF_RANGE


def test_parse_repeated_second_is_refused() -> None:
    assert problem_of("second,percent\n1,100\n1,90\n") is ProblemKind.REPEATED


def test_parse_lists_every_bad_row_up_to_the_report_limit() -> None:
    rows = "\n".join(f"{index},bad" for index in range(30))
    with pytest.raises(RetentionCsvError) as refused:
        parse_retention_csv("second,percent\n" + rows)
    assert len(refused.value.problems) == 20


def test_parse_text_over_size_limit_is_refused() -> None:
    assert problem_of("second,percent\n" + "0,1\n" * MAX_CSV_CHARS) is ProblemKind.TOO_LARGE


def test_parse_more_rows_than_the_limit_is_refused() -> None:
    rows = "\n".join(f"{index},1" for index in range(MAX_RETENTION_POINTS + 1))
    assert problem_of("second,percent\n" + rows) is ProblemKind.ROWS


def test_problem_message_is_english_with_row_and_field_filled_in() -> None:
    with pytest.raises(RetentionCsvError, match=r"Row 2: percent must be a number\."):
        parse_retention_csv("second,percent\n0,x")
