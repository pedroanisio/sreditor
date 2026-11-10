"""Reading the short script form: numbered rows of blocks and on-screen texts (web.forms.errors).

Each block row posts block-<i>-<field>, each text row text-<i>-<field>. A block posts how long
it lasts, not when it starts: blocks follow one another from 0 s, so their times always chain,
and a block left without a length gets one from its words. A row whose speech or text is empty
is left out. A block's beats are rows inside its row, block-<i>-beat-<j>-<field>, and a beat
row without a character is left out, so the blank row at the end adds one. Buttons named
`action` add, move or remove one row (row_action), and a block carries its beats when it moves.
Every error is keyed by the field it belongs to and already translated.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sreditor.i18n import N_, Translator
from sreditor.short_script import (
    WORDS_PER_SECOND,
    Beat,
    Block,
    Impact,
    ScreenText,
    ShortScriptInput,
    beat_problem,
    count_words,
)
from sreditor.voice_markup import PROBLEM_MESSAGES, problems
from sreditor.web.forms import field_errors, message_for

BLOCK_PREFIX = "block"
TEXT_PREFIX = "text"
# How an on-screen text is shown: at its own times, or for the whole video.
SHOWN_TIMED = "timed"
SHOWN_THROUGHOUT = "throughout"
NUMBER_EXPECTED = N_("Enter a number of seconds, such as 2.5.")
BLOCK_TOO_SHORT = N_("A block must last more than 0 seconds.")
# A blank length is estimated at the slower speaking rate, rounded up to half a second, so the
# estimate leaves room rather than cutting speech short.
ESTIMATE_STEP = 0.5
MIN_ESTIMATE_SECONDS = 1.0
TIME_DIGITS = 3
TEXT_ENDS_BEFORE_START = N_("On-screen text must start before it ends.")
INTENSITY_EXPECTED = N_("Pick how strong the reaction is.")
BEAT_PREFIX = "beat"
BEATS_FIELD = "beats"
# Checkbox groups post one value per ticked box; the page joins them with this separator.
LIST_SEPARATOR = ","
BEAT_TIME_EXPECTED = N_("Enter when the beat starts, in seconds from the start of the block.")

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


def beat_name(position: int, field: str) -> str:
    """The name of one field of one beat, inside its block row."""
    return f"{BEAT_PREFIX}-{position}-{field}"


def wear_of(value: str) -> list[str]:
    """The items a beat wears, from the ticked boxes; unknown ones are left for the model to refuse."""
    return [item for item in value.split(LIST_SEPARATOR) if item]


def beat_values(reader: RowReader, position: int) -> dict[str, object] | None:
    """One beat row as model input, or None when it names no character."""
    character = reader.text(beat_name(position, "character"))
    if not character:
        return None
    at = reader.number(beat_name(position, "at_s"))
    if at is None and reader.name(beat_name(position, "at_s")) not in reader.errors:
        reader.errors[reader.name(beat_name(position, "at_s"))] = reader.translate(
            BEAT_TIME_EXPECTED
        )
    return {
        "at_s": at,
        "character": character,
        "eyes": reader.text(beat_name(position, "eyes")),
        "mouth": reader.text(beat_name(position, "mouth")),
        "arms": reader.text(beat_name(position, "arms")),
        "wear": wear_of(reader.text(beat_name(position, "wear"))),
        "prop": reader.text(beat_name(position, "prop")) or None,
        "action": reader.text(beat_name(position, "action")),
    }


def beat_of(reader: RowReader, position: int) -> Beat | None:
    """One beat row, or None when it is blank or has errors, which go next to its fields."""
    values = beat_values(reader, position)
    if values is None or reader.name(beat_name(position, "at_s")) in reader.errors:
        return None
    try:
        return Beat.model_validate(values)
    except ValidationError as invalid:
        for field, message in field_errors(invalid, reader.translate).items():
            # A whole-beat refusal (an action without a prop) belongs next to the prop.
            name = beat_name(position, "prop" if field == "form" else field)
            reader.errors.setdefault(reader.name(name), message)
        return None


def beats_of(reader: RowReader, length: float | None) -> list[Beat]:
    """The block's beats, in order; their timing is checked against the block's length."""
    found = [
        beat
        for position in range(row_count_of(reader, f"{BEAT_PREFIX}_count"))
        if (beat := beat_of(reader, position)) is not None
    ]
    problem = beat_problem(found, length) if length is not None else None
    if problem:
        reader.errors[reader.name(BEATS_FIELD)] = reader.translate(problem)
    return found


def row_count_of(reader: RowReader, field: str) -> int:
    """A count a row posts about its own inner rows."""
    count = reader.text(field)
    return int(count) if count.isdigit() else 0


def estimated_seconds(speech: str) -> float:
    """How long a speech takes at the slower speaking rate, in half seconds, at least 1 s."""
    slowest = min(WORDS_PER_SECOND)
    steps = math.ceil(count_words(speech) / slowest / ESTIMATE_STEP)
    return max(MIN_ESTIMATE_SECONDS, steps * ESTIMATE_STEP)


def length_of(reader: RowReader, speech: str) -> float | None:
    """The block's length as typed, or estimated when blank; None, with an error, when wrong."""
    length = reader.number("duration_s")
    if length is None and reader.name("duration_s") not in reader.errors:
        return estimated_seconds(speech)
    if length is not None and length <= 0:
        reader.errors[reader.name("duration_s")] = reader.translate(BLOCK_TOO_SHORT)
        return None
    return length


def block_of(reader: RowReader, start: float, length: float | None) -> Block | None:
    """One block row starting at `start`, or None when it has errors."""
    speech = reader.text("speech")
    markup_errors(reader, speech)
    impact = impact_of(reader)
    beats = beats_of(reader, length)
    if reader.errors or length is None:
        return None
    try:
        return Block.model_validate(
            {
                "start_s": start,
                "end_s": round(start + length, TIME_DIGITS),
                "function": reader.text("function"),
                "speech": speech,
                "impact": impact,
                "speaker": reader.text("speaker") or None,
                "place": reader.text("place") or None,
                "beats": beats,
            }
        )
    except ValidationError as invalid:
        reader.refuse(invalid)
        return None


def read_blocks(form: Mapping[str, str], translate: Translator) -> tuple[list[Block], Errors]:
    """Every block with speech, each starting where the one before it ends."""
    found: list[Block] = []
    errors: Errors = {}
    clock = 0.0
    for index in range(row_count(form, BLOCK_PREFIX)):
        reader = RowReader(form, BLOCK_PREFIX, index, translate)
        speech = reader.text("speech")
        if not speech:
            continue
        length = length_of(reader, speech)
        block = block_of(reader, clock, length)
        errors.update(reader.errors)
        clock = round(clock + (length or 0), TIME_DIGITS)
        if block is not None:
            found.append(block)
    return found, errors


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
    blocks, block_errors = read_blocks(form, translate)
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


def sent_rows(sent: Mapping[str, str], prefix: str) -> list[dict[str, str]]:
    """The rows a form posted, in order, each as its fields."""
    count = row_count(sent, prefix)
    marker = f"{prefix}-"
    rows: list[dict[str, str]] = [{} for _ in range(count)]
    for name, value in sent.items():
        if name.startswith(marker):
            index, _, field = name[len(marker) :].partition("-")
            if index.isdigit() and int(index) < count:
                rows[int(index)][field] = value
    return rows


def with_rows(sent: Mapping[str, str], prefix: str, rows: list[dict[str, str]]) -> dict[str, str]:
    """The form with one kind of rows replaced, renumbered from 0."""
    marker = f"{prefix}-"
    kept = {name: value for name, value in sent.items() if not name.startswith(marker)}
    kept[f"{prefix}_count"] = str(len(rows))
    for index, row in enumerate(rows):
        kept |= {row_name(prefix, index, field): value for field, value in row.items()}
    return kept


class RowAction(BaseModel):
    """What a row button asks: the kind of rows, the change and the row it applies to."""

    model_config = ConfigDict(frozen=True)

    prefix: str
    verb: str
    index: int


ADD = "add"
REMOVE = "remove"
UP = "up"
DOWN = "down"
VERBS = (ADD, REMOVE, UP, DOWN)
# An action reads "<verb>-<prefix>-<index>", such as "up-block-2"; adding at -1 adds on top.
ACTION_FIELD = "action"
ACTION_PATTERN = re.compile(rf"({'|'.join(VERBS)})-({BLOCK_PREFIX}|{TEXT_PREFIX})-(-?\d+)")


def row_action(sent: Mapping[str, str]) -> RowAction | None:
    """The row button that sent the form, or None for a plain save."""
    found = ACTION_PATTERN.fullmatch(sent.get(ACTION_FIELD, ""))
    if found is None:
        return None
    verb, prefix, index = found.groups()
    return RowAction(prefix=prefix, verb=verb, index=int(index))


def moved(rows: list[dict[str, str]], action: RowAction) -> list[dict[str, str]]:
    """The rows after the action; a row that cannot move stays where it is."""
    changed = list(rows)
    index = action.index
    if action.verb == ADD:
        changed.insert(max(index + 1, 0), {"is_new": "1"})
    elif action.verb == REMOVE and 0 <= index < len(changed):
        del changed[index]
    elif action.verb == UP and 0 < index < len(changed):
        changed[index - 1], changed[index] = changed[index], changed[index - 1]
    elif action.verb == DOWN and 0 <= index < len(changed) - 1:
        changed[index], changed[index + 1] = changed[index + 1], changed[index]
    return changed


def apply_row_action(sent: Mapping[str, str], action: RowAction) -> dict[str, str]:
    """The form as it would be after the action, without the action itself."""
    rest = {name: value for name, value in sent.items() if name != ACTION_FIELD}
    return with_rows(rest, action.prefix, moved(sent_rows(sent, action.prefix), action))
