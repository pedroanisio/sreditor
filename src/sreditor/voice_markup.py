"""Voice direction written inline in a short script's speech (rule project.voice-markup).

A tag marks a point, [pause:0.3], or a span, [irony]official term[/irony]. The vocabulary below
is the only definition of the tags: each says what it does and how ElevenLabs renders it in
each model. v2 models take SSML <break> pauses and nothing else; v3 takes audio tags, which
have no closing form and last until the next tag. v3 has no pause of a given length (its [pause]
lasts what the model decides), so v3 receives none: `timed_pauses` says where each one goes and
the silence is put into the audio afterwards (audio_pauses.py). Camera, sound and edit cues render in no
model: they stay in the script for the editor and are never spoken.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sreditor.i18n import N_

TAG_PATTERN = re.compile(r"\[(/?)([a-z_]+)(?::([^\[\]]+))?\]")
SECONDS_PATTERN = re.compile(r"\d+(\.\d+)?")
# v3 audio tags passed through as written: plain English words only.
AUDIO_TAG_PATTERN = re.compile(r"[A-Za-z][A-Za-z '\-]*")
SPACES = re.compile(r"\s+")
WORD = re.compile(r"\w")
SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.!?;:])")
# ElevenLabs refuses a <break> longer than 3 seconds.
MAX_PAUSE_SECONDS = 3.0
VALUE_SLOT = "{value}"


class TtsModel(StrEnum):
    """The ElevenLabs model families the markup renders for."""

    V2 = "v2"
    V3 = "v3"


class TagClass(StrEnum):
    """What a tag changes; sound and visual cues are for the editor, never spoken."""

    RHYTHM = "rhythm"
    VOICE = "voice"
    EMOTION = "emotion"
    REACTION = "reaction"
    SOUND = "sound"
    VISUAL = "visual"
    PASSTHROUGH = "passthrough"


EDIT_CLASSES = frozenset({TagClass.SOUND, TagClass.VISUAL})


class TagForm(StrEnum):
    """A point tag stands alone; a span tag opens and closes around text."""

    POINT = "point"
    SPAN = "span"


class ValueKind(StrEnum):
    """What follows the colon in [tag:value]."""

    NONE = "none"
    SECONDS = "seconds"
    NAME = "name"


class Tag(BaseModel):
    """One tag of the vocabulary: its form, its value and how each model renders it."""

    model_config = ConfigDict(frozen=True)

    name: str
    tag_class: TagClass
    form: TagForm
    effect: str  # an N_ msgid, shown to people choosing a tag
    value: ValueKind = ValueKind.NONE
    choices: tuple[str, ...] = ()
    v2: str | None = None
    v3: str | None = None
    is_upper_case_in_v3: bool = False  # v3 reads CAPITALS as emphasis

    def rendering(self, model: TtsModel) -> str | None:
        """What the model receives for this tag, with {value} still to fill; None when nothing."""
        return self.v2 if model == TtsModel.V2 else self.v3

    @property
    def is_spoken(self) -> bool:
        """Whether any model turns the tag into sound."""
        return bool(self.v2 or self.v3 or self.is_upper_case_in_v3)


BREAK = '<break time="{value}s" />'


def span(name: str, tag_class: TagClass, effect: str, v3: str) -> Tag:
    """A span tag that only v3 renders, as an audio tag at its start."""
    return Tag(name=name, tag_class=tag_class, form=TagForm.SPAN, effect=effect, v3=v3)


def point(name: str, tag_class: TagClass, effect: str, v3: str | None = None) -> Tag:
    """A point tag without a value; v3 renders it when given."""
    return Tag(name=name, tag_class=tag_class, form=TagForm.POINT, effect=effect, v3=v3)


TAGS: tuple[Tag, ...] = (
    Tag(
        name="pause",
        tag_class=TagClass.RHYTHM,
        form=TagForm.POINT,
        value=ValueKind.SECONDS,
        effect=N_("A breath before the information, giving weight to what follows."),
        v2=BREAK,
    ),
    Tag(
        name="silence",
        tag_class=TagClass.RHYTHM,
        form=TagForm.POINT,
        value=ValueKind.SECONDS,
        effect=N_("Total silence that lets the last sentence echo."),
        v2=BREAK,
    ),
    span("fast", TagClass.RHYTHM, N_("Rushes the span, like a contract's fine print."), "[rushed]"),
    span("slow", TagClass.RHYTHM, N_("Spells out the absurd."), "[slowly]"),
    span("drawn_out", TagClass.RHYTHM, N_("Stretches the words out."), "[drawn out]"),
    Tag(
        name="emphasis",
        tag_class=TagClass.VOICE,
        form=TagForm.SPAN,
        effect=N_("Stresses the word that carries the joke or the figure."),
        is_upper_case_in_v3=True,
    ),
    span(
        "irony",
        TagClass.VOICE,
        N_("Spoken quotes: the official term said as a euphemism."),
        "[sarcastically]",
    ),
    span("whisper", TagClass.VOICE, N_("A secret tone: nobody can know."), "[whispers]"),
    span("soft", TagClass.VOICE, N_("A soft voice."), "[softly]"),
    span("shout", TagClass.VOICE, N_("A shout."), "[shouts]"),
    span("quiet", TagClass.VOICE, N_("Low volume, without whispering."), "[quietly]"),
    span("cautious", TagClass.VOICE, N_("Walking on eggshells."), "[cautiously]"),
    span("dismissive", TagClass.VOICE, N_("Dismissive: big deal."), "[dismissive]"),
    span("cheeky", TagClass.VOICE, N_("Cheeky, with a wink."), "[cheeky]"),
    Tag(
        name="accent",
        tag_class=TagClass.VOICE,
        form=TagForm.SPAN,
        value=ValueKind.NAME,
        effect=N_("An accent, named in English (French, British…)."),
        v3="[{value} accent]",
    ),
    Tag(
        name="character",
        tag_class=TagClass.VOICE,
        form=TagForm.SPAN,
        value=ValueKind.NAME,
        effect=N_("A character voice, named in English (pirate…)."),
        v3="[{value} voice]",
    ),
    span("sad", TagClass.EMOTION, N_("Sadness."), "[sad]"),
    span("angry", TagClass.EMOTION, N_("Open anger."), "[angry]"),
    span("happy", TagClass.EMOTION, N_("Joy, good for restrained irony."), "[happily]"),
    span("grief", TagClass.EMOTION, N_("Mourning, lament."), "[sorrowful]"),
    span("excited", TagClass.EMOTION, N_("A salesperson's excitement."), "[excited]"),
    span("worried", TagClass.EMOTION, N_("Worry."), "[worried]"),
    span("awe", TagClass.EMOTION, N_("Amazement."), "[awe]"),
    point("laugh", TagClass.REACTION, N_("A laugh."), "[laughs]"),
    point("chuckle", TagClass.REACTION, N_("A small chuckle."), "[chuckles]"),
    point("sigh", TagClass.REACTION, N_("A here-we-go sigh."), "[sighs]"),
    point(
        "clear_throat",
        TagClass.REACTION,
        N_("Clears the throat before the figure."),
        "[clears throat]",
    ),
    point("cough", TagClass.REACTION, N_("A cough."), "[coughing]"),
    Tag(
        name="voice_sfx",
        tag_class=TagClass.REACTION,
        form=TagForm.POINT,
        value=ValueKind.NAME,
        choices=("gunshot", "explosion", "clapping"),
        effect=N_("A sound effect made by the v3 voice itself (gunshot, explosion, clapping)."),
        v3="[{value}]",
    ),
    Tag(
        name="sfx",
        tag_class=TagClass.SOUND,
        form=TagForm.POINT,
        value=ValueKind.NAME,
        effect=N_("A sound effect added in the edit (stamp, cash register, siren…)."),
    ),
    Tag(
        name="music",
        tag_class=TagClass.SOUND,
        form=TagForm.POINT,
        value=ValueKind.NAME,
        effect=N_("Music starts, changes or stops in the edit (use stop to cut it)."),
    ),
    point("cut", TagClass.VISUAL, N_("A hard cut.")),
    point("zoom", TagClass.VISUAL, N_("A punch-in on the face or object.")),
    point("look_at_camera", TagClass.VISUAL, N_("The narrator looks into the camera.")),
    Tag(
        name="tts",
        tag_class=TagClass.PASSTHROUGH,
        form=TagForm.POINT,
        value=ValueKind.NAME,
        effect=N_("Passes any other v3 audio tag through: [tts:mischievously]."),
        v3="[{value}]",
    ),
)
VOCABULARY: dict[str, Tag] = {tag.name: tag for tag in TAGS}


class ProblemKind(StrEnum):
    """Why a tag is refused."""

    UNKNOWN = "unknown"
    POINT_CLOSED = "point_closed"
    CLOSED_UNOPENED = "closed_unopened"
    VALUE_NOT_TAKEN = "value_not_taken"
    VALUE_MISSING = "value_missing"
    VALUE_NOT_LISTED = "value_not_listed"
    AUDIO_TAG = "audio_tag"
    SECONDS = "seconds"
    UNCLOSED = "unclosed"


PROBLEM_MESSAGES: dict[ProblemKind, str] = {
    ProblemKind.UNKNOWN: N_("[%(tag)s] is not a voice tag; pick one from the list."),
    ProblemKind.POINT_CLOSED: N_("[%(tag)s] marks a point and has no closing tag."),
    ProblemKind.CLOSED_UNOPENED: N_("[/%(tag)s] closes a span that was never opened."),
    ProblemKind.VALUE_NOT_TAKEN: N_("[%(tag)s] takes no value."),
    ProblemKind.VALUE_MISSING: N_("[%(tag)s] needs a value, as in [%(tag)s:…]."),
    ProblemKind.VALUE_NOT_LISTED: N_("[%(tag)s] takes one of: %(choices)s."),
    ProblemKind.AUDIO_TAG: N_("[%(tag)s] passes plain English words only."),
    ProblemKind.SECONDS: N_("[%(tag)s] takes seconds between 0 and 3, such as 0.5."),
    ProblemKind.UNCLOSED: N_("[%(tag)s] is opened and never closed."),
}


class MarkupProblem(BaseModel):
    """A tag the vocabulary refuses, with what is wrong."""

    model_config = ConfigDict(frozen=True)

    kind: ProblemKind
    tag: str
    choices: str = ""

    def message(self) -> str:
        """The problem in English, for the API."""
        return PROBLEM_MESSAGES[self.kind] % {"tag": self.tag, "choices": self.choices}


def value_problem(tag: Tag, value: str | None) -> ProblemKind | None:
    """What is wrong with an opening tag's value, if anything."""
    if tag.value == ValueKind.NONE:
        return ProblemKind.VALUE_NOT_TAKEN if value is not None else None
    if value is None:
        return ProblemKind.VALUE_MISSING
    if tag.choices and value not in tag.choices:
        return ProblemKind.VALUE_NOT_LISTED
    if tag.tag_class == TagClass.PASSTHROUGH and not AUDIO_TAG_PATTERN.fullmatch(value):
        return ProblemKind.AUDIO_TAG
    if tag.value == ValueKind.SECONDS and not is_pause_length(value):
        return ProblemKind.SECONDS
    return None


def is_pause_length(value: str) -> bool:
    """Seconds above 0 and at most what ElevenLabs accepts."""
    return bool(SECONDS_PATTERN.fullmatch(value)) and 0 < float(value) <= MAX_PAUSE_SECONDS


def closing_problem(tag: Tag, open_spans: list[str]) -> ProblemKind | None:
    """What is wrong with a closing tag; pops its span when it is right."""
    if tag.form == TagForm.POINT:
        return ProblemKind.POINT_CLOSED
    if not open_spans or open_spans[-1] != tag.name:
        return ProblemKind.CLOSED_UNOPENED
    open_spans.pop()
    return None


def problems(speech: str) -> list[MarkupProblem]:
    """Every refused tag in the speech: unknown, wrongly valued, or not closed in order."""
    found: list[MarkupProblem] = []
    open_spans: list[str] = []
    for match in TAG_PATTERN.finditer(speech):
        is_closing, name, value = match.group(1) == "/", match.group(2), match.group(3)
        tag = VOCABULARY.get(name)
        if tag is None:
            found.append(MarkupProblem(kind=ProblemKind.UNKNOWN, tag=name))
            continue
        if is_closing:
            kind = closing_problem(tag, open_spans)
        else:
            kind = value_problem(tag, value)
            if tag.form == TagForm.SPAN:
                open_spans.append(name)
        if kind is not None:
            found.append(MarkupProblem(kind=kind, tag=name, choices=", ".join(tag.choices)))
    found += [MarkupProblem(kind=ProblemKind.UNCLOSED, tag=name) for name in open_spans]
    return found


def tidy(text: str) -> str:
    """Single spaces, and none before punctuation."""
    return SPACE_BEFORE_PUNCTUATION.sub(r"\1", SPACES.sub(" ", text)).strip()


def plain_text(speech: str) -> str:
    """The speech without any tags, as a person reads it."""
    return tidy(TAG_PATTERN.sub(" ", speech))


class SpeechPause(BaseModel):
    """A pause or silence the speech asks for: how many spoken words come before it, and how long."""

    model_config = ConfigDict(frozen=True)

    after_words: int
    seconds: float


def spoken_word_count(text: str) -> int:
    """Words as the speech's timestamps count them: runs between spaces holding a letter or digit."""
    return sum(1 for run in text.split() if any(character.isalnum() for character in run))


def timed_pauses(speech: str) -> list[SpeechPause]:
    """Each pause and silence of the speech, placed by the spoken words before it."""
    found: list[SpeechPause] = []
    words = 0
    position = 0
    for match in TAG_PATTERN.finditer(speech):
        words += spoken_word_count(speech[position : match.start()])
        position = match.end()
        tag = VOCABULARY.get(match.group(2))
        value = match.group(3)
        if tag and tag.value == ValueKind.SECONDS and value and is_pause_length(value):
            found.append(SpeechPause(after_words=words, seconds=float(value)))
    return found


def pause_seconds(speech: str) -> float:
    """The seconds of pause and silence the speech asks for."""
    return sum(pause.seconds for pause in timed_pauses(speech))


def upper_case_outside_tags(span_text: str) -> str:
    """CAPITALS for the words of a span, leaving the tags inside it as they are."""
    parts: list[str] = []
    position = 0
    for match in TAG_PATTERN.finditer(span_text):
        parts += [span_text[position : match.start()].upper(), match.group(0)]
        position = match.end()
    return "".join(parts) + span_text[position:].upper()


def with_upper_case_spans(speech: str, model: TtsModel) -> str:
    """Spans the model reads as CAPITALS, rewritten so; their tags disappear later."""
    if model != TtsModel.V3:
        return speech
    for tag in TAGS:
        if tag.is_upper_case_in_v3:
            pattern = re.compile(rf"\[{tag.name}\](.*?)\[/{tag.name}\]")
            speech = pattern.sub(lambda found: upper_case_outside_tags(found.group(1)), speech)
    return speech


def for_speech(speech: str, model: TtsModel) -> str:
    """The text ElevenLabs receives: each tag as the model renders it, or removed."""

    def render(match: re.Match[str]) -> str:
        tag = VOCABULARY.get(match.group(2))
        rendering = tag.rendering(model) if tag else None
        if match.group(1) == "/" or not rendering:
            return " "
        return f" {rendering.replace(VALUE_SLOT, match.group(3) or '')} "

    return tidy(TAG_PATTERN.sub(render, with_upper_case_spans(speech, model)))


def lost_in_v2(speeches: list[str]) -> list[str]:
    """Voice tags used that v2 cannot render, so they vanish from its audio."""
    names = {
        match.group(2)
        for speech in speeches
        for match in TAG_PATTERN.finditer(speech)
        if not match.group(1)
    }
    return sorted(
        name
        for name in names
        if name in VOCABULARY
        and not VOCABULARY[name].v2
        and VOCABULARY[name].tag_class not in EDIT_CLASSES
    )


def changes_v3_voice(name: str) -> bool:
    """Whether the tag switches the v3 voice until the next tag, since v3 has no closing tags."""
    tag = VOCABULARY.get(name)
    if tag is None or not tag.v3:
        return False
    return tag.form == TagForm.SPAN or tag.tag_class == TagClass.PASSTHROUGH


def leaks_in_v3(speeches: list[str]) -> list[str]:
    """Spans whose v3 voice goes on past their closing tag, into the words that follow."""
    text = " ".join(speeches)
    leaking: list[str] = []
    pending: str | None = None
    position = 0
    for match in TAG_PATTERN.finditer(text):
        if pending and WORD.search(text[position : match.start()]):
            leaking.append(pending)
            pending = None
        position = match.end()
        is_closing, name = match.group(1) == "/", match.group(2)
        if changes_v3_voice(name):
            pending = name if is_closing else None
    if pending and WORD.search(text[position:]):
        leaking.append(pending)
    return leaking
