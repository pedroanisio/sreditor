"""The beats editor inside each short-script block: rows of poses for srstudio (rule project.srstudio.beats).

A block's beats post as block-<i>-beat-<j>-<field>, with beat_count saying how many rows the page
showed. The page always ends with a blank row, so filling it adds a beat and clearing a row's
character drops it, with no JavaScript. Poses come from srstudio's closed toon vocabularies and
are shown by their srstudio names, as the tag guide shows voice tags.
"""

from __future__ import annotations

from typing import Any

from sreditor.deps import Services
from sreditor.i18n import N_, Translator
from sreditor.library import LibraryKind
from sreditor.short_script import Beat
from sreditor.srstudio_operations import all_entries
from sreditor.srstudio_vocabulary import Arms, Eyes, Mouth, Wear
from sreditor.web.workspace.script_form import (
    BEAT_PREFIX,
    BLOCK_PREFIX,
    LIST_SEPARATOR,
    beat_name,
    wear_of,
)

BEAT_FIELDS = ("at_s", "character", "eyes", "mouth", "arms", "wear", "prop", "action")
COUNT_FIELD = f"{BEAT_PREFIX}_count"
NO_BEAT = N_("No beat")
NO_PROP = N_("No prop")


def stored_beat_fields(beats: list[Beat]) -> dict[str, str]:
    """A block's saved beats as the form fields its row posts."""
    fields = {COUNT_FIELD: str(len(beats))}
    for position, beat in enumerate(beats):
        fields |= {
            beat_name(position, "at_s"): f"{beat.at_s:g}",
            beat_name(position, "character"): str(beat.character),
            beat_name(position, "eyes"): beat.eyes.value,
            beat_name(position, "mouth"): beat.mouth.value,
            beat_name(position, "arms"): beat.arms.value,
            beat_name(position, "wear"): LIST_SEPARATOR.join(item.value for item in beat.wear),
            beat_name(position, "prop"): beat.prop or "",
            beat_name(position, "action"): beat.action,
        }
    return fields


def beat_rows(row: dict[str, str]) -> list[dict[str, Any]]:
    """The beats a block row holds, as typed or saved, then the blank row that adds one."""
    count = row.get(COUNT_FIELD, "0")
    shown: list[dict[str, Any]] = []
    for position in range(int(count) if count.isdigit() else 0):
        values: dict[str, Any] = {
            field: row.get(beat_name(position, field), "") for field in BEAT_FIELDS
        }
        if values["character"]:
            values["wear"] = wear_of(values["wear"])
            shown.append(values)
    blank: dict[str, Any] = {field: "" for field in BEAT_FIELDS}
    return [*shown, {**blank, "wear": [], "at_s": "" if shown else "0"}]


def names(values: type[Eyes | Mouth | Arms | Wear]) -> list[tuple[str, str]]:
    """A vocabulary's values, each shown by its srstudio name."""
    return [(item.value, item.value) for item in values]


def beat_choices(app: Services, channel: str, translate: Translator) -> dict[str, Any]:
    """What each beat select offers: the channel's characters and props, and the toon poses."""
    characters = all_entries(app, channel, LibraryKind.CHARACTER)
    props = all_entries(app, channel, LibraryKind.PROP)
    prop_sheets = app.srstudio.sheets(props)
    return {
        "beat_characters": [
            ("", translate(NO_BEAT)),
            *((str(entry.id), entry.name) for entry in characters),
        ],
        "beat_props": [
            ("", translate(NO_PROP)),
            *((prop_sheets[entry.id].slug, entry.name) for entry in props),
        ],
        "eyes_options": names(Eyes),
        "mouth_options": names(Mouth),
        "arms_options": names(Arms),
        "wear_options": names(Wear),
    }


def blocks_with_beat_errors(errors: dict[str, str]) -> set[int]:
    """The block rows whose beats have an error, so their beats open on the page."""
    marker = f"-{BEAT_PREFIX}"
    found: set[int] = set()
    for name in errors:
        block, separator, rest = name.removeprefix(f"{BLOCK_PREFIX}-").partition("-")
        if separator and block.isdigit() and rest.startswith(marker[1:]):
            found.add(int(block))
    return found
