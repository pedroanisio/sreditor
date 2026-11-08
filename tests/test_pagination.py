"""Paging rows held in memory with an opaque cursor (rule agentic.pagination)."""

from __future__ import annotations

import pytest

from sreditor.pagination import decode_cursor, encode_cursor, page_after
from sreditor.problems import ProblemError

NAMES = ["carol", "alice", "bob"]


def same(name: str) -> str:
    return name


def test_page_after_without_cursor_starts_at_the_first_in_order() -> None:
    first = page_after(NAMES, None, 2, same)
    assert first.items == ["alice", "bob"]
    assert decode_cursor(first.next_cursor) == "bob"


def test_page_after_with_cursor_continues_after_it() -> None:
    rest = page_after(NAMES, encode_cursor("bob"), 2, same)
    assert rest.items == ["carol"]
    assert rest.next_cursor is None


def test_decode_cursor_invalid_names_the_fix() -> None:
    with pytest.raises(ProblemError) as refused:
        decode_cursor("not a cursor!")
    assert "next_cursor" in refused.value.detail
