"""On-screen texts move with their blocks when measured voice times replace the planned ones."""

from __future__ import annotations

from sreditor.screen_retiming import moved_screen_texts, moved_time
from sreditor.short_script import Block, BlockFunction, ScreenText

PLANNED = [
    Block(start_s=0, end_s=2, function=BlockFunction.HOOK, speech="Oi."),
    Block(start_s=2, end_s=6, function=BlockFunction.PUNCH, speech="Tchau."),
]
MEASURED = [
    Block(start_s=0, end_s=1, function=BlockFunction.HOOK, speech="Oi."),
    Block(start_s=1, end_s=3, function=BlockFunction.PUNCH, speech="Tchau."),
]


def test_moved_time_inside_block_keeps_its_share() -> None:
    assert moved_time(4, PLANNED, MEASURED) == 2


def test_moved_time_past_last_block_keeps_its_distance_from_the_end() -> None:
    assert moved_time(7, PLANNED, MEASURED) == 4


def test_moved_time_in_gap_goes_to_next_block_start() -> None:
    gapped = [PLANNED[0], PLANNED[1].model_copy(update={"start_s": 3})]
    assert moved_time(2.5, gapped, MEASURED) == 1


def test_moved_screen_texts_timed_text_follows_its_block() -> None:
    text = ScreenText(text="Tchau!", start_s=2, end_s=6)
    moved = moved_screen_texts([text], PLANNED, MEASURED)[0]
    assert (moved.start_s, moved.end_s) == (1, 3)


def test_moved_screen_texts_persistent_text_stays() -> None:
    text = ScreenText(text="@funnymemes", is_persistent=True)
    assert moved_screen_texts([text], PLANNED, MEASURED) == [text]
