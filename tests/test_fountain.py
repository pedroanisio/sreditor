"""Reading scenes and characters from a Fountain screenplay."""

from __future__ import annotations

import pytest

from sreditor.fountain import (
    Block,
    BlockKind,
    blocks,
    character_name,
    outline,
    scene_heading,
    scene_location,
)

PILOT = """Title: Pilot
Author: Ana

INT. KITCHEN - DAY

Ana stares at the calendar.

ANA
Is it Monday again?

BRUNO (V.O.)
Every week.

CUT TO:

EXT. BUS STOP - MORNING #2#

ANA (CONT'D)
Still Monday.
"""


def test_outline_lists_scenes_in_order_without_numbers() -> None:
    assert outline(PILOT).scenes == ["INT. KITCHEN - DAY", "EXT. BUS STOP - MORNING"]


def test_outline_lists_each_speaker_once_without_extensions() -> None:
    assert outline(PILOT).characters == ["ANA", "BRUNO"]


def test_outline_lists_each_place_once_without_time_of_day() -> None:
    text = PILOT + "\nINT. KITCHEN - NIGHT\n\nShe sleeps.\n"
    assert outline(text).locations == ["KITCHEN", "BUS STOP"]


@pytest.mark.parametrize(
    ("heading", "place"),
    [
        ("INT. KITCHEN - DAY", "KITCHEN"),
        ("INT./EXT. CAR - NIGHT", "CAR"),
        ("EXT. ROOFTOP", "ROOFTOP"),
        ("FLASHBACK - 1999", "FLASHBACK"),
        ("INT. - DAY", None),
    ],
)
def test_scene_location_drops_prefix_and_time(heading: str, place: str | None) -> None:
    assert scene_location(heading) == place


def test_outline_of_empty_text_is_empty() -> None:
    assert outline("").scenes == [] and outline("").characters == []


def test_outline_skips_title_page_without_body() -> None:
    assert outline("Title: Pilot\nAuthor: Ana").characters == []


def test_outline_ignores_boneyard_and_notes() -> None:
    text = "/*\nINT. CUT SCENE - NIGHT\n*/\n\nINT. HALL - DAY [[check the set]]\n"
    assert outline(text).scenes == ["INT. HALL - DAY"]


def test_outline_capitals_followed_by_blank_line_is_no_character() -> None:
    assert outline("THE END\n\nRoll credits.").characters == []


def test_outline_heading_inside_paragraph_is_no_scene() -> None:
    assert outline("She thinks.\nINT. NOWHERE\n").scenes == []


@pytest.mark.parametrize(
    ("line", "heading"),
    [
        ("int. office - day", "int. office - day"),
        ("INT./EXT. CAR - NIGHT", "INT./EXT. CAR - NIGHT"),
        ("I/E CAR", "I/E CAR"),
        (".FLASHBACK", "FLASHBACK"),
        ("...and then", None),
        ("INTERIOR", None),
        (".", None),
    ],
)
def test_scene_heading_recognises_prefixes_and_forced_headings(
    line: str, heading: str | None
) -> None:
    assert scene_heading(line) == heading


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("@McCLANE", "McCLANE"),
        ("HANS (on the radio)", "HANS"),
        ("BRICK ^", "BRICK"),
        ("R2D2", "R2D2"),
        ("Ana", None),
        ("!LOUD NOISE", None),
        ("> FADE OUT", None),
        ("123", None),
    ],
)
def test_character_name_reads_cues_and_refuses_other_lines(line: str, name: str | None) -> None:
    assert character_name(line) == name


def test_blocks_format_scene_action_and_dialogue() -> None:
    text = "INT. KITCHEN - DAY\nAna sits.\n\nANA\n(sighing)\nIs it Monday?\n"
    assert blocks(text) == [
        Block(kind=BlockKind.SCENE, text="INT. KITCHEN - DAY"),
        Block(kind=BlockKind.ACTION, text="Ana sits."),
        Block(kind=BlockKind.CHARACTER, text="ANA"),
        Block(kind=BlockKind.PARENTHETICAL, text="(sighing)"),
        Block(kind=BlockKind.DIALOGUE, text="Is it Monday?"),
    ]


@pytest.mark.parametrize(
    ("line", "kind", "text"),
    [
        ("CUT TO:", BlockKind.TRANSITION, "CUT TO:"),
        ("> FADE OUT.", BlockKind.TRANSITION, "FADE OUT."),
        ("> THE END <", BlockKind.CENTERED, "THE END"),
        ("!SILENCE", BlockKind.ACTION, "SILENCE"),
        ("A lone line.", BlockKind.ACTION, "A lone line."),
    ],
)
def test_blocks_single_line_takes_its_kind(line: str, kind: BlockKind, text: str) -> None:
    # A first line such as "CUT TO:" would read as a title page key, so action comes first.
    assert blocks(f"Ana runs.\n\n{line}")[-1] == Block(kind=kind, text=text)


def test_blocks_forced_character_drops_the_at_sign() -> None:
    assert blocks("@McCLANE\nYippee.")[0] == Block(kind=BlockKind.CHARACTER, text="McCLANE")


def test_blocks_skip_title_page_and_blank_runs() -> None:
    assert blocks("Title: Pilot\n\n\n\nEXT. ROOF - NIGHT") == [
        Block(kind=BlockKind.SCENE, text="EXT. ROOF - NIGHT")
    ]
