"""Move on-screen texts with the blocks when measured voice times replace the planned ones.

A text's times are its own, not a block's, so applying a take's timings would leave it where the
old plan put it, over the wrong words. Each time is carried by the block it falls in, at the same
share of that block; a time in a gap goes to the start of the next block, and one past the last
block keeps its distance from that block's end.
"""

from __future__ import annotations

from sreditor.short_script import MAX_DURATION_SECONDS, Block, ScreenText

# Times are kept to the hundredth of a second, finer than any editor shows.
TIME_DECIMALS = 2


def moved_time(time: float, planned: list[Block], measured: list[Block]) -> float:
    """Where a time lands once each planned block runs at its measured times."""
    for old, new in zip(planned, measured, strict=True):
        if time <= old.start_s:
            return new.start_s
        if time <= old.end_s:
            share = (time - old.start_s) / (old.end_s - old.start_s)
            return new.start_s + share * (new.end_s - new.start_s)
    if not planned:
        return time
    return measured[-1].end_s + time - planned[-1].end_s


def moved_text(text: ScreenText, planned: list[Block], measured: list[Block]) -> ScreenText:
    """One on-screen text at its new times; a persistent or untimed text stays as it is."""
    if text.start_s is None:
        return text
    start = min(
        round(moved_time(text.start_s, planned, measured), TIME_DECIMALS), MAX_DURATION_SECONDS
    )
    if text.end_s is None:
        return text.model_copy(update={"start_s": start})
    end = round(moved_time(text.end_s, planned, measured), TIME_DECIMALS)
    if end <= start:
        # Both ends fell in one gap or an empty block: keep the text's length.
        end = round(start + text.end_s - text.start_s, TIME_DECIMALS)
    return text.model_copy(update={"start_s": start, "end_s": min(end, MAX_DURATION_SECONDS)})


def moved_screen_texts(
    texts: list[ScreenText], planned: list[Block], measured: list[Block]
) -> list[ScreenText]:
    """The script's on-screen texts, moved with the blocks from planned to measured times."""
    return [moved_text(text, planned, measured) for text in texts]
