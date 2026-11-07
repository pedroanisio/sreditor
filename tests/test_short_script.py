from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from sreditor.short_script import (
    Block,
    ScreenText,
    ShortScriptInput,
    WarningKind,
    count_words,
    warnings_of,
)


def block(
    start: float, end: float, function: str, speech: str = "Words.", **more: Any
) -> dict[str, Any]:
    return {"start_s": start, "end_s": end, "function": function, "speech": speech, **more}


PUNCH_IMPACT = {"reaction": "laughter", "intensity": 4}
GOOD_BLOCKS = [
    block(0, 3, "hook"),
    block(3, 10, "turn"),
    block(10, 15, "punch", impact=PUNCH_IMPACT),
]


def script(blocks: list[dict[str, Any]] | None = None, **more: Any) -> ShortScriptInput:
    return ShortScriptInput.model_validate(
        {"duration_s": 15, "blocks": GOOD_BLOCKS if blocks is None else blocks, **more}
    )


def warning_kinds(written: ShortScriptInput) -> list[WarningKind]:
    return [found.kind for found in warnings_of(written)]


def test_warnings_of_well_shaped_script_is_empty() -> None:
    assert warnings_of(script()) == []


def test_warnings_of_empty_script_is_empty() -> None:
    assert warnings_of(ShortScriptInput()) == []


def test_warnings_of_gap_between_blocks_names_the_block() -> None:
    blocks = [block(0, 3, "hook"), block(4, 15, "punch", impact=PUNCH_IMPACT)]
    found = warnings_of(script(blocks))
    assert [item.kind for item in found] == [WarningKind.GAP]
    assert found[0].message == "Block 2 starts at 4.0 s; the one before ends at 3.0 s."


def test_warnings_of_blocks_ending_early_reports_it() -> None:
    assert warning_kinds(script(duration_s=20)) == [WarningKind.ENDS_EARLY]


def test_warnings_of_script_not_opening_with_hook_reports_it() -> None:
    blocks = [block(0, 3, "development"), block(3, 15, "punch", impact=PUNCH_IMPACT)]
    assert warning_kinds(script(blocks)) == [WarningKind.NO_HOOK]


def test_warnings_of_slow_hook_reports_it() -> None:
    blocks = [block(0, 5, "hook"), block(5, 15, "punch", impact=PUNCH_IMPACT)]
    assert warning_kinds(script(blocks)) == [WarningKind.SLOW_HOOK]


def test_warnings_of_script_ending_without_punch_reports_it() -> None:
    blocks = [block(0, 3, "hook"), block(3, 15, "development")]
    assert warning_kinds(script(blocks)) == [WarningKind.NO_PUNCH]


def test_warnings_of_cta_after_punch_is_fine() -> None:
    blocks = [*GOOD_BLOCKS[:2], block(10, 13, "punch", impact=PUNCH_IMPACT), block(13, 15, "cta")]
    assert warnings_of(script(blocks)) == []


def test_warnings_of_punch_without_reaction_reports_it() -> None:
    blocks = [*GOOD_BLOCKS[:2], block(10, 15, "punch")]
    assert warning_kinds(script(blocks)) == [WarningKind.PUNCH_WITHOUT_IMPACT]


def test_warnings_of_far_too_many_words_reports_them() -> None:
    crowded = [
        *GOOD_BLOCKS[:2],
        block(10, 15, "punch", " ".join(["word"] * 60), impact=PUNCH_IMPACT),
    ]
    assert warning_kinds(script(crowded)) == [WarningKind.TOO_MANY_WORDS]


def test_warnings_of_words_just_over_pace_says_at_limit() -> None:
    near = [*GOOD_BLOCKS[:2], block(10, 15, "punch", " ".join(["word"] * 40), impact=PUNCH_IMPACT)]
    assert warning_kinds(script(near)) == [WarningKind.WORDS_AT_LIMIT]


def test_warnings_of_pauses_leave_less_time_for_words() -> None:
    paused = [
        *GOOD_BLOCKS[:2],
        block(10, 15, "punch", "[pause:3] " + " ".join(["word"] * 37), impact=PUNCH_IMPACT),
    ]
    assert warning_kinds(script(paused)) == [WarningKind.TOO_MANY_WORDS]


def test_warnings_of_untimed_screen_text_reports_it() -> None:
    written = script(screen_texts=[{"text": "R$ 12 bi"}])
    assert warning_kinds(written) == [WarningKind.TEXT_UNTIMED]


def test_warnings_of_screen_text_past_the_end_reports_it() -> None:
    written = script(screen_texts=[{"text": "Late", "start_s": 14, "end_s": 18}])
    assert warning_kinds(written) == [WarningKind.TEXT_OUTSIDE]


def test_warnings_of_v2_with_voice_tags_lists_lost_tags() -> None:
    blocks = [block(0, 3, "hook", "[irony]a[/irony]"), *GOOD_BLOCKS[1:]]
    written = script(blocks, voice={"model": "v2"})
    assert warning_kinds(written) == [WarningKind.LOST_IN_V2]


def test_warnings_of_v3_span_leaking_reports_it() -> None:
    blocks = [block(0, 3, "hook", "[irony]a[/irony] b"), *GOOD_BLOCKS[1:]]
    assert warning_kinds(script(blocks)) == [WarningKind.LEAKS_IN_V3]


def test_block_ending_before_it_starts_is_refused() -> None:
    with pytest.raises(ValidationError, match="must end after it starts"):
        Block.model_validate(block(3, 3, "hook"))


def test_block_with_unknown_tag_is_refused_naming_it() -> None:
    with pytest.raises(ValidationError, match=r"\[shrug\] is not a voice tag"):
        Block.model_validate(block(0, 3, "hook", "A [shrug]."))


def test_screen_text_over_ninety_characters_is_refused() -> None:
    with pytest.raises(ValidationError):
        ScreenText(text="x" * 91)


def test_screen_text_ending_without_start_is_refused() -> None:
    with pytest.raises(ValidationError, match="start before it ends"):
        ScreenText(text="Hi", end_s=3)


def test_voice_with_bad_language_is_refused() -> None:
    with pytest.raises(ValidationError):
        ShortScriptInput.model_validate({"voice": {"language": "Portuguese"}})


def test_count_words_counts_each_digit_of_a_number() -> None:
    assert count_words("Paid 116 [pause:0.3] reais") == 5
