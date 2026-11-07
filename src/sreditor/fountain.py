"""Reading a screenplay written in Fountain (https://fountain.io) for its scenes, places and cast.

Only what sreditor shows is read: scene headings, the places they name and the characters who
speak. Any text is valid Fountain, so reading never fails; lines it cannot place are action.
"""

from __future__ import annotations

import re

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
