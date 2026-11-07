"""Reading a screenplay written in Fountain (https://fountain.io) for its scenes, places and cast.

Only what sreditor shows is read: scene headings, the places they name, the characters who
speak, and the blocks of the formatted preview. Any text is valid Fountain, so reading never
fails; lines it cannot place are action.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

# Scene headings start with one of these, then a dot or a space (INT. KITCHEN - DAY).
SCENE_PREFIX = re.compile(r"^(INT\./EXT|INT/EXT|I/E|INT|EXT|EST)[. ]", re.IGNORECASE)
SCENE_NUMBER = re.compile(r"\s*#[^#]*#$")
FORCED_SCENE = "."
FORCED_CHARACTER = "@"
# A character's extension, such as (V.O.) or (CONT'D), and the caret of dual dialogue.
CHARACTER_EXTENSION = re.compile(r"\s*(\([^)]*\)\s*)*\^?$")
TITLE_KEY = re.compile(r"^[A-Za-z][A-Za-z ]*:")
BONEYARD = re.compile(r"/\*.*?\*/", re.DOTALL)
NOTE = re.compile(r"\[\[.*?\]\]", re.DOTALL)
# Lines forced to be action, centered text, a transition or lyrics are never a character.
NOT_A_CHARACTER = ("!", ">", "~", "=", "#")
# A heading names its place before the time of day: INT. KITCHEN - DAY.
TIME_SEPARATOR = " - "
FORCED_ACTION = "!"
FORCED_TRANSITION = ">"
CENTERED_END = "<"
TRANSITION_END = "TO:"
PARENTHETICAL = re.compile(r"^\(.*\)$")


class Outline(BaseModel):
    """What a screenplay holds: its scenes in order, then its places and who speaks, once each."""

    model_config = ConfigDict(frozen=True)

    scenes: list[str] = []
    locations: list[str] = []
    characters: list[str] = []


def body_lines(text: str) -> list[str]:
    """The screenplay's lines without its title page, comments (boneyard) and notes."""
    lines = NOTE.sub("", BONEYARD.sub("", text)).replace("\r\n", "\n").split("\n")
    if lines and TITLE_KEY.match(lines[0]):
        end = lines.index("") if "" in lines else len(lines)
        return lines[end:]
    return lines


def scene_heading(line: str) -> str | None:
    """The heading a line declares, without its scene number, or None."""
    stripped = line.strip()
    if stripped.startswith(FORCED_SCENE) and not stripped.startswith(FORCED_SCENE * 2):
        stripped = stripped[1:].strip()
    elif not SCENE_PREFIX.match(stripped):
        return None
    return SCENE_NUMBER.sub("", stripped) or None


def scene_location(heading: str) -> str | None:
    """The place a scene heading names, without INT./EXT. and the time of day."""
    place = SCENE_PREFIX.sub("", heading, count=1).split(TIME_SEPARATOR)[0].strip()
    return place or None


def character_name(line: str) -> str | None:
    """The character a cue names, without extensions, or None when the line is no cue."""
    stripped = line.strip()
    is_forced = stripped.startswith(FORCED_CHARACTER)
    name = CHARACTER_EXTENSION.sub("", stripped.removeprefix(FORCED_CHARACTER)).strip()
    if not any(char.isalpha() for char in name):
        return None
    # Only the name must be in capitals; an extension such as (on the radio) may not be.
    is_cue = name == name.upper() and not stripped.startswith(NOT_A_CHARACTER)
    return name if is_forced or is_cue else None


def outline(text: str) -> Outline:
    """The scenes and speaking characters of a Fountain screenplay."""
    lines = body_lines(text)
    scenes: list[str] = []
    characters: dict[str, None] = {}  # keeps first-appearance order
    for index, line in enumerate(lines):
        follows_blank = index == 0 or not lines[index - 1].strip()
        if not follows_blank or not line.strip():
            continue
        if heading := scene_heading(line):
            scenes.append(heading)
            continue
        speaks = index + 1 < len(lines) and bool(lines[index + 1].strip())
        if speaks and (name := character_name(line)):
            characters.setdefault(name)
    places = (scene_location(scene) for scene in scenes)
    locations = dict.fromkeys(place for place in places if place)
    return Outline(scenes=scenes, locations=list(locations), characters=list(characters))


class BlockKind(StrEnum):
    """What a block of the formatted screenplay is; each kind has its own layout."""

    SCENE = "scene"
    ACTION = "action"
    CHARACTER = "character"
    PARENTHETICAL = "parenthetical"
    DIALOGUE = "dialogue"
    TRANSITION = "transition"
    CENTERED = "centered"


class ScreenplayBlock(BaseModel):
    """One block of the formatted screenplay: a heading, a paragraph of action, a cue, a line."""

    model_config = ConfigDict(frozen=True)

    kind: BlockKind
    text: str


def paragraphs(text: str) -> list[list[str]]:
    """The screenplay's body split at blank lines, each paragraph as its lines."""
    found: list[list[str]] = [[]]
    for line in body_lines(text):
        if line.strip():
            found[-1].append(line.strip())
        elif found[-1]:
            found.append([])
    return [paragraph for paragraph in found if paragraph]


def single_line_block(line: str) -> ScreenplayBlock | None:
    """A transition or centered line standing alone, or None."""
    if line.startswith(FORCED_TRANSITION) and line.endswith(CENTERED_END):
        return ScreenplayBlock(kind=BlockKind.CENTERED, text=line[1:-1].strip())
    if line.startswith(FORCED_TRANSITION):
        return ScreenplayBlock(kind=BlockKind.TRANSITION, text=line[1:].strip())
    if line == line.upper() and line.endswith(TRANSITION_END):
        return ScreenplayBlock(kind=BlockKind.TRANSITION, text=line)
    return None


def dialogue_blocks(lines: list[str]) -> list[ScreenplayBlock]:
    """A cue and what follows it: the character, then parentheticals and lines of dialogue."""
    blocks = [
        ScreenplayBlock(kind=BlockKind.CHARACTER, text=lines[0].removeprefix(FORCED_CHARACTER))
    ]
    for line in lines[1:]:
        kind = BlockKind.PARENTHETICAL if PARENTHETICAL.match(line) else BlockKind.DIALOGUE
        blocks.append(ScreenplayBlock(kind=kind, text=line))
    return blocks


def paragraph_blocks(lines: list[str]) -> list[ScreenplayBlock]:
    """The blocks one paragraph of the screenplay formats as."""
    first = lines[0]
    if heading := scene_heading(first):
        rest = (
            [ScreenplayBlock(kind=BlockKind.ACTION, text="\n".join(lines[1:]))] if lines[1:] else []
        )
        return [ScreenplayBlock(kind=BlockKind.SCENE, text=heading), *rest]
    if len(lines) == 1 and (alone := single_line_block(first)):
        return [alone]
    if len(lines) > 1 and character_name(first):
        return dialogue_blocks(lines)
    action = "\n".join(line.removeprefix(FORCED_ACTION) for line in lines)
    return [ScreenplayBlock(kind=BlockKind.ACTION, text=action)]


def blocks(text: str) -> list[ScreenplayBlock]:
    """The screenplay as blocks in order, for a preview formatted like a printed script."""
    return [block for paragraph in paragraphs(text) for block in paragraph_blocks(paragraph)]
