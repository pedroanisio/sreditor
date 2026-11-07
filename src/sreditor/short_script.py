"""An episode's short script for vertical video, stored and revised on its own (project.short-script).

Blocks run in order from start_s to end_s; each has a function, speech with voice markup
(project.voice-markup) and, optionally, the reaction it should cause. On-screen texts carry the
video without sound. What would make the script unusable (broken markup, a block ending before
it starts) is refused; what only makes it weaker (a gap, a slow hook, no final punch, too many
words for the time) comes back as warnings, so a draft can always be saved.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sreditor.database import Database
from sreditor.episode_texts import EpisodeTexts, PartChanged, TextKey, TextPart
from sreditor.i18n import N_
from sreditor.srstudio_vocabulary import (
    MIN_BEAT_STEP_SECONDS,
    SLUG_PATTERN,
    Arms,
    Eyes,
    Mouth,
    Wear,
)
from sreditor.voice_markup import (
    TtsModel,
    leaks_in_v3,
    lost_in_v2,
    pause_seconds,
    plain_text,
    problems,
)

SPEECH_LENGTH = 2000
SCREEN_TEXT_LENGTH = 90
NOTE_LENGTH = 300
MAX_BLOCKS = 60
MAX_SCREEN_TEXTS = 40
MAX_BEATS = 40
ACTION_LENGTH = 40
# Float sums such as 0.1 + 0.05 fall a hair short of 0.15; srstudio allows the same slack.
TIME_SLACK = 1e-9
MAX_DURATION_SECONDS = 600
DEFAULT_DURATION_SECONDS = 60
MIN_INTENSITY = 1
MAX_INTENSITY = 5
DEFAULT_LANGUAGE = "pt-BR"
# Languages offered for the speech, each named in itself; the API takes any BCP 47 tag.
SPEECH_LANGUAGES = {
    "pt-BR": "Português (Brasil)",
    "pt-PT": "Português (Portugal)",
    "en-US": "English (US)",
    "en-GB": "English (UK)",
    "es": "Español",
}


def speech_language_for(channel_language: str) -> str:
    """The listed speech language closest to the channel's: same tag, else same language."""
    wanted = channel_language.lower()
    for tag in SPEECH_LANGUAGES:
        if tag.lower() == wanted:
            return tag
    primary = wanted.split("-")[0]
    return next(
        (tag for tag in SPEECH_LANGUAGES if tag.lower().split("-")[0] == primary),
        DEFAULT_LANGUAGE,
    )


# A hook must catch the scroll within the first seconds.
HOOK_SECONDS = 3
# Narration pace: about 35 to 40 words in 15 seconds.
WORDS_PER_SECOND = (2.3, 2.7)
WORD_TOLERANCE = 0.15
WORD = re.compile(r"\d+(?:[.,]\d+)*|[\w$]+")
VOICE_ID_PATTERN = r"^[A-Za-z0-9]{0,64}$"
LANGUAGE_PATTERN = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$"

Speech = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SPEECH_LENGTH)
]
ScreenWords = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SCREEN_TEXT_LENGTH)
]
Seconds = Annotated[float, Field(ge=0, le=MAX_DURATION_SECONDS)]


class BlockFunction(StrEnum):
    """What a block does for the video, in the order a short usually runs."""

    HOOK = "hook"
    DEVELOPMENT = "development"
    TURN = "turn"
    PUNCH = "punch"
    CTA = "cta"


class Reaction(StrEnum):
    """What the audience should feel at a block."""

    CURIOSITY = "curiosity"
    RECOGNITION = "recognition"
    TENSION = "tension"
    SURPRISE = "surprise"
    OUTRAGE = "outrage"
    LAUGHTER = "laughter"
    DISCOMFORT = "discomfort"


class Impact(BaseModel):
    """The reaction a block aims for, and how strongly."""

    model_config = ConfigDict(frozen=True)

    reaction: Reaction
    intensity: int = Field(ge=MIN_INTENSITY, le=MAX_INTENSITY)
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=NOTE_LENGTH)] = ""


class Beat(BaseModel):
    """One held pose of a cast character from `at_s` seconds into its block (project.srstudio.beats)."""

    model_config = ConfigDict(frozen=True)

    at_s: Seconds
    character: UUID
    eyes: Eyes
    mouth: Mouth
    arms: Arms
    wear: list[Wear] = Field(default_factory=list, max_length=len(Wear))
    prop: Annotated[str, StringConstraints(pattern=SLUG_PATTERN)] | None = None
    action: Annotated[str, StringConstraints(strip_whitespace=True, max_length=ACTION_LENGTH)] = ""

    @model_validator(mode="after")
    def is_well_formed(self) -> Self:
        """Each item is worn once, and an action needs the prop it happens to."""
        if len(set(self.wear)) != len(self.wear):
            raise ValueError(N_("A beat wears each item at most once."))
        if self.action and self.prop is None:
            raise ValueError(N_("A prop action needs a prop."))
        return self


def beat_problem(beats: list[Beat], length: float) -> str | None:
    """Why a block's beats cannot be drawn: the first at 0, each 0.15 s after the one before, all inside."""
    if beats and beats[0].at_s != 0:
        return N_("The first beat of a block starts at 0 s.")
    steps = pairwise(beats)
    if any(
        later.at_s < earlier.at_s + MIN_BEAT_STEP_SECONDS - TIME_SLACK for earlier, later in steps
    ):
        return N_("Beats must be at least 0.15 s apart.")
    if beats and beats[-1].at_s >= length:
        return N_("Every beat starts before its block ends.")
    return None


class Block(BaseModel):
    """A stretch of the video: when it runs, what it does, who says what, and how the cast moves."""

    model_config = ConfigDict(frozen=True)

    start_s: Seconds
    end_s: Seconds
    function: BlockFunction
    speech: Speech
    impact: Impact | None = None
    # A character of the channel's library; None is the episode's narrator voice.
    speaker: UUID | None = None
    beats: list[Beat] = Field(default_factory=list, max_length=MAX_BEATS)

    @model_validator(mode="after")
    def is_well_formed(self) -> Self:
        """A block ends after it starts, its markup is in the vocabulary and its beats can be drawn."""
        if self.end_s <= self.start_s:
            raise ValueError(N_("A block must end after it starts."))
        found = problems(self.speech)
        if found:
            raise ValueError(found[0].message())
        beat_error = beat_problem(self.beats, self.end_s - self.start_s)
        if beat_error:
            raise ValueError(beat_error)
        return self


class ScreenText(BaseModel):
    """Words on screen, shown for a while or the whole video."""

    model_config = ConfigDict(frozen=True)

    text: ScreenWords
    start_s: Seconds | None = None
    end_s: Seconds | None = None
    is_persistent: bool = False

    @model_validator(mode="after")
    def has_sensible_times(self) -> Self:
        """An end needs a start before it."""
        if self.end_s is not None and (self.start_s is None or self.end_s <= self.start_s):
            raise ValueError(N_("On-screen text must start before it ends."))
        return self


class VoiceChoice(BaseModel):
    """Who speaks the script and how: the ElevenLabs model, voice and language."""

    model_config = ConfigDict(frozen=True)

    model: TtsModel = TtsModel.V3
    voice_id: Annotated[str, StringConstraints(pattern=VOICE_ID_PATTERN)] = ""
    language: Annotated[str, StringConstraints(pattern=LANGUAGE_PATTERN)] = DEFAULT_LANGUAGE


class ShortScriptInput(BaseModel):
    """The whole short script, as written."""

    model_config = ConfigDict(frozen=True)

    duration_s: float = Field(default=DEFAULT_DURATION_SECONDS, gt=0, le=MAX_DURATION_SECONDS)
    voice: VoiceChoice = VoiceChoice()
    blocks: list[Block] = Field(default_factory=list, max_length=MAX_BLOCKS)
    screen_texts: list[ScreenText] = Field(default_factory=list, max_length=MAX_SCREEN_TEXTS)


class WarningKind(StrEnum):
    """What makes a script weaker without making it unusable."""

    GAP = "gap"
    ENDS_EARLY = "ends_early"
    NO_HOOK = "no_hook"
    SLOW_HOOK = "slow_hook"
    NO_PUNCH = "no_punch"
    PUNCH_WITHOUT_IMPACT = "punch_without_impact"
    TOO_MANY_WORDS = "too_many_words"
    WORDS_AT_LIMIT = "words_at_limit"
    TEXT_UNTIMED = "text_untimed"
    TEXT_OUTSIDE = "text_outside"
    LOST_IN_V2 = "lost_in_v2"
    LEAKS_IN_V3 = "leaks_in_v3"


WARNING_MESSAGES: dict[WarningKind, str] = {
    WarningKind.GAP: N_(
        "Block %(block)s starts at %(start)s s; the one before ends at %(expected)s s."
    ),
    WarningKind.ENDS_EARLY: N_("The blocks end at %(end)s s, but the video lasts %(duration)s s."),
    WarningKind.NO_HOOK: N_("The first block should be the hook."),
    WarningKind.SLOW_HOOK: N_("The hook runs until %(end)s s; keep it within 3 s."),
    WarningKind.NO_PUNCH: N_("The last spoken block should be the punch."),
    WarningKind.PUNCH_WITHOUT_IMPACT: N_(
        "Block %(block)s is a punch without an expected reaction."
    ),
    WarningKind.TOO_MANY_WORDS: N_(
        "%(words)s words do not fit in %(seconds)s s of speech (about %(most)s at most)."
    ),
    WarningKind.WORDS_AT_LIMIT: N_(
        "%(words)s words in %(seconds)s s of speech is at the limit (best %(least)s to %(most)s)."
    ),
    WarningKind.TEXT_UNTIMED: N_(
        'On-screen text "%(text)s" has no time and is not shown throughout.'
    ),
    WarningKind.TEXT_OUTSIDE: N_('On-screen text "%(text)s" runs past the end of the video.'),
    WarningKind.LOST_IN_V2: N_("The v2 model drops these voice tags: %(tags)s."),
    WarningKind.LEAKS_IN_V3: N_(
        "In v3, [%(tags)s] keeps going after it closes, since v3 tags have no end."
    ),
}


class ScriptWarning(BaseModel):
    """A weakness of the script, with the values its message names."""

    model_config = ConfigDict(frozen=True)

    kind: WarningKind
    message: str
    values: dict[str, str] = Field(default_factory=dict)


def warning(kind: WarningKind, **values: object) -> ScriptWarning:
    """A warning with its English message filled in."""
    shown = {name: str(value) for name, value in values.items()}
    return ScriptWarning(kind=kind, message=WARNING_MESSAGES[kind] % shown, values=shown)


class ShortScript(ShortScriptInput):
    """An episode's short script with its revision and the warnings it raises."""

    channel: str
    number: int
    revision: int
    updated_at: datetime | None = None
    warnings: list[ScriptWarning] = Field(default_factory=list)


def count_words(speech: str) -> int:
    """Spoken words; a number counts one per digit, since 116 takes as long as three words."""
    return sum(
        sum(character.isdigit() for character in token) if token[0].isdigit() else 1
        for token in WORD.findall(plain_text(speech))
    )


def timing_warnings(script: ShortScriptInput) -> list[ScriptWarning]:
    """Gaps and overlaps between blocks, and blocks that stop before the video does."""
    found: list[ScriptWarning] = []
    cursor = 0.0
    for index, block in enumerate(script.blocks, start=1):
        if block.start_s != cursor:
            found.append(
                warning(WarningKind.GAP, block=index, start=block.start_s, expected=cursor)
            )
        cursor = block.end_s
    if script.blocks and cursor != script.duration_s:
        found.append(warning(WarningKind.ENDS_EARLY, end=cursor, duration=script.duration_s))
    return found


def shape_warnings(blocks: list[Block]) -> list[ScriptWarning]:
    """A short opens with a quick hook and lands on a punch before any call to action."""
    if not blocks:
        return []
    found: list[ScriptWarning] = []
    if blocks[0].function != BlockFunction.HOOK:
        found.append(warning(WarningKind.NO_HOOK))
    elif blocks[0].end_s > HOOK_SECONDS:
        found.append(warning(WarningKind.SLOW_HOOK, end=blocks[0].end_s))
    spoken = [block for block in blocks if block.function != BlockFunction.CTA]
    if not spoken or spoken[-1].function != BlockFunction.PUNCH:
        found.append(warning(WarningKind.NO_PUNCH))
    found += [
        warning(WarningKind.PUNCH_WITHOUT_IMPACT, block=index)
        for index, block in enumerate(blocks, start=1)
        if block.function == BlockFunction.PUNCH and block.impact is None
    ]
    return found


def pace_warnings(script: ShortScriptInput) -> list[ScriptWarning]:
    """Too many words for the time left after pauses."""
    words = sum(count_words(block.speech) for block in script.blocks)
    seconds = script.duration_s - sum(pause_seconds(block.speech) for block in script.blocks)
    least, most = (rate * seconds for rate in WORDS_PER_SECOND)
    values = {
        "words": words,
        "seconds": f"{seconds:.1f}",
        "least": round(least),
        "most": round(most),
    }
    if words > most * (1 + WORD_TOLERANCE):
        return [warning(WarningKind.TOO_MANY_WORDS, **values)]
    if words > most:
        return [warning(WarningKind.WORDS_AT_LIMIT, **values)]
    return []


def screen_warnings(script: ShortScriptInput) -> list[ScriptWarning]:
    """On-screen texts that never show, or show past the end."""
    found: list[ScriptWarning] = []
    for item in script.screen_texts:
        if item.start_s is None and not item.is_persistent:
            found.append(warning(WarningKind.TEXT_UNTIMED, text=item.text))
        elif max(item.start_s or 0, item.end_s or 0) > script.duration_s:
            found.append(warning(WarningKind.TEXT_OUTSIDE, text=item.text))
    return found


def voice_warnings(script: ShortScriptInput) -> list[ScriptWarning]:
    """Tags the chosen model drops or cannot end."""
    speeches = [block.speech for block in script.blocks]
    if script.voice.model == TtsModel.V2:
        lost = lost_in_v2(speeches)
        return [warning(WarningKind.LOST_IN_V2, tags=", ".join(lost))] if lost else []
    return [warning(WarningKind.LEAKS_IN_V3, tags=name) for name in leaks_in_v3(speeches)]


def warnings_of(script: ShortScriptInput) -> list[ScriptWarning]:
    """Everything that weakens the script, in reading order."""
    return [
        *timing_warnings(script),
        *shape_warnings(script.blocks),
        *pace_warnings(script),
        *screen_warnings(script),
        *voice_warnings(script),
    ]


def short_script_key(channel: str, number: int) -> TextKey:
    """Where an episode's short script is stored."""
    return TextKey(part=TextPart.SHORT_SCRIPT, channel=channel, number=number)


class ShortScriptStore:
    """Short scripts as JSON in the application database; deleting the episode deletes them."""

    def __init__(self, database: Database) -> None:
        self.texts = EpisodeTexts(database)

    def short_script(self, channel: str, number: int) -> ShortScript:
        """The episode's short script, empty at revision 0 when nobody has written it."""
        stored = self.texts.read(short_script_key(channel, number))
        content = (
            ShortScriptInput.model_validate_json(stored.text) if stored.text else ShortScriptInput()
        )
        return ShortScript(
            **content.model_dump(),
            channel=channel,
            number=number,
            revision=stored.revision,
            updated_at=stored.updated_at,
            warnings=warnings_of(content),
        )

    def write(
        self, channel: str, number: int, data: ShortScriptInput, expected_revision: int | None
    ) -> ShortScript:
        """Replace the short script; with `expected_revision`, only if it is still at it."""
        key = short_script_key(channel, number)
        if self.texts.write(key, data.model_dump_json(), expected_revision) is None:
            current = self.texts.read(key).revision
            raise PartChanged(f"Short script of {channel} #{number}", current)
        return self.short_script(channel, number)
