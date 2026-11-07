"""Reading the short script form: numbered rows of blocks and on-screen texts (web.forms.errors).

Each block row posts block-<i>-<field>, each text row text-<i>-<field>. A row whose speech or
text is empty is left out, so clearing it removes it and the spare row at the end adds one.
Every error is keyed by the field it belongs to and already translated.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sreditor.i18n import N_, Translator
from sreditor.short_script import Block, Impact, ScreenText, ShortScriptInput
from sreditor.voice_markup import PROBLEM_MESSAGES, problems
from sreditor.web.forms import field_errors, message_for

BLOCK_PREFIX = "block"
TEXT_PREFIX = "text"
# How an on-screen text is shown: at its own times, or for the whole video.
SHOWN_TIMED = "timed"
SHOWN_THROUGHOUT = "throughout"
NUMBER_EXPECTED = N_("Enter a number of seconds, such as 2.5.")
ENDS_BEFORE_START = N_("A block must end after it starts.")
TEXT_ENDS_BEFORE_START = N_("On-screen text must start before it ends.")
INTENSITY_EXPECTED = N_("Pick how strong the reaction is.")
# Beats have no editor on the page yet: they ride along as JSON so saving the page keeps them.
BEATS_UNREADABLE = N_("The beats of this block could not be read. Reload the page.")

Errors = dict[str, str]


class ParsedScript(BaseModel):
    """The script read from the form, or the errors that kept it from being read."""

    model_config = ConfigDict(frozen=True)

    script: ShortScriptInput | None = None
    errors: Errors = Field(default_factory=dict)


def row_name(prefix: str, index: int, field: str) -> str:
    """The form name of one field of one row."""
    return f"{prefix}-{index}-{field}"


def row_count(form: Mapping[str, str], prefix: str) -> int:
    """How many rows of a kind the form posted."""
    count = form.get(f"{prefix}_count", "0")
    return int(count) if count.isdigit() else 0


def seconds(value: str) -> float | None:
    """A number of seconds a person typed, accepting a comma for the decimals; None if blank."""
    text = value.strip().replace(",", ".")
    return float(text) if text else None


class RowReader:
    """Reads one row's fields and collects their errors."""

    def __init__(
        self, form: Mapping[str, str], prefix: str, index: int, translate: Translator
    ) -> None:
        self.form = form
        self.prefix = prefix
        self.index = index
        self.translate = translate
        self.errors: Errors = {}

    def name(self, field: str) -> str:
        """This row's form name for a field."""
        return row_name(self.prefix, self.index, field)

    def text(self, field: str) -> str:
        """A field's text, trimmed."""
        return self.form.get(self.name(field), "").strip()

    def number(self, field: str) -> float | None:
        """A field's seconds; records an error when it is not a number."""
        try:
            return seconds(self.form.get(self.name(field), ""))
        except ValueError:
            self.errors[self.name(field)] = self.translate(NUMBER_EXPECTED)
            return None

    def refuse(self, invalid: ValidationError) -> None:
        """Place a model's errors next to this row's fields."""
        for field, message in field_errors(invalid, self.translate).items():
            self.errors.setdefault(self.name(field), message)


def markup_errors(reader: RowReader, speech: str) -> None:
    """The first refused voice tag of the speech, translated, next to it."""
    found = problems(speech)
    if found:
        first = found[0]
        message = reader.translate(
            PROBLEM_MESSAGES[first.kind], tag=first.tag, choices=first.choices
        )
        reader.errors[reader.name("speech")] = message


def impact_of(reader: RowReader) -> Impact | None:
    """The reaction a block aims for, when one is picked."""
    reaction = reader.text("reaction")
    if not reaction:
        return None
    intensity = reader.text("intensity")
    if not intensity.isdigit():
        reader.errors[reader.name("intensity")] = reader.translate(INTENSITY_EXPECTED)
        return None
    try:
        return Impact.model_validate(
            {"reaction": reaction, "intensity": int(intensity), "note": reader.text("note")}
        )
    except ValidationError as invalid:
        reader.refuse(invalid)
        return None


def beats_of(reader: RowReader) -> list[Any]:
    """The block's beats as the page carried them; records an error when they are not JSON."""
    try:
        beats = json.loads(reader.text("beats") or "[]")
    except json.JSONDecodeError:
        beats = None
    if not isinstance(beats, list):
        reader.errors[reader.name("speech")] = reader.translate(BEATS_UNREADABLE)
        return []
    return beats


def block_of(reader: RowReader) -> Block | None:
    """One block row, or None when it is empty or has errors."""
    speech = reader.text("speech")
    if not speech:
        return None
    start, end = reader.number("start_s"), reader.number("end_s")
    markup_errors(reader, speech)
    if start is not None and end is not None and end <= start:
        reader.errors[reader.name("end_s")] = reader.translate(ENDS_BEFORE_START)
    impact = impact_of(reader)
    beats = beats_of(reader)
    if reader.errors:
        return None
    try:
        return Block.model_validate(
            {
                "start_s": start,
                "end_s": end,
                "function": reader.text("function"),
                "speech": speech,
                "impact": impact,
                "speaker": reader.text("speaker") or None,
                "beats": beats,
            }
        )
    except ValidationError as invalid:
        reader.refuse(invalid)
        return None


def screen_text_of(reader: RowReader) -> ScreenText | None:
    """One on-screen text row, or None when it is empty or has errors."""
    words = reader.text("text")
    if not words:
        return None
    start, end = reader.number("start_s"), reader.number("end_s")
    if end is not None and (start is None or end <= start):
        reader.errors[reader.name("end_s")] = reader.translate(TEXT_ENDS_BEFORE_START)
    if reader.errors:
        return None
    try:
        return ScreenText.model_validate(
            {
                "text": words,
                "start_s": start,
                "end_s": end,
                "is_persistent": reader.text("shown") == SHOWN_THROUGHOUT,
            }
        )
    except ValidationError as invalid:
        reader.refuse(invalid)
        return None


def read_rows[T](
    form: Mapping[str, str],
    prefix: str,
    translate: Translator,
    read: Callable[[RowReader], T | None],
) -> tuple[list[T], Errors]:
    """Every non-empty row of a kind, with the errors of all rows."""
    found: list[T] = []
    errors: Errors = {}
    for index in range(row_count(form, prefix)):
        reader = RowReader(form, prefix, index, translate)
        item = read(reader)
        errors.update(reader.errors)
        if item is not None:
            found.append(item)
    return found, errors


def settings_of(form: Mapping[str, str], translate: Translator) -> tuple[dict[str, object], Errors]:
    """The script's length and voice as typed, with the length's error if any."""
    errors: Errors = {}
    try:
        duration = seconds(form.get("duration_s", ""))
    except ValueError:
        duration = None
        errors["duration_s"] = translate(NUMBER_EXPECTED)
    voice = {
        "model": form.get("model", ""),
        "voice_id": form.get("voice_id", "").strip(),
        "language": form.get("language", "").strip(),
    }
    values: dict[str, object] = {"voice": voice}
    if duration is not None:
        values["duration_s"] = duration
    return values, errors


def parse_script(form: Mapping[str, str], translate: Translator) -> ParsedScript:
    """The whole script from the form, or every error found in it."""
    values, errors = settings_of(form, translate)
    blocks, block_errors = read_rows(form, BLOCK_PREFIX, translate, block_of)
    texts, text_errors = read_rows(form, TEXT_PREFIX, translate, screen_text_of)
    errors |= block_errors | text_errors
    if errors:
        return ParsedScript(errors=errors)
    try:
        script = ShortScriptInput.model_validate(
            {**values, "blocks": blocks, "screen_texts": texts}
        )
    except ValidationError as invalid:
        return ParsedScript(errors=voice_errors(invalid, translate))
    return ParsedScript(script=script)


def voice_errors(invalid: ValidationError, translate: Translator) -> Errors:
    """The script's own errors, next to the length or the voice field they concern."""
    errors: Errors = {}
    for error in invalid.errors():
        location = [str(part) for part in error["loc"]]
        name = (
            location[-1]
            if location and location[0] == "voice"
            else (location[0] if location else "form")
        )
        errors.setdefault(name, message_for(dict(error), translate))
    return errors
