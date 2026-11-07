"""An episode's short script page: timed blocks, on-screen texts and the generated voice-over.

The whole script is one form, so it saves as one revision (project.short-script). Owners and
editors also see the voice-over panel: generate a take with ElevenLabs, listen to it and use
its measured times (project.voice-generation).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, TypeAdapter

from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.elevenlabs import ElevenLabsError, Voice
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.library import LibraryKind
from sreditor.members import Permission
from sreditor.short_script import (
    MAX_INTENSITY,
    MIN_INTENSITY,
    SCREEN_TEXT_LENGTH,
    SPEECH_LANGUAGES,
    SPEECH_LENGTH,
    WARNING_MESSAGES,
    Beat,
    BlockFunction,
    Reaction,
    ScriptWarning,
    ShortScript,
)
from sreditor.short_script_operations import (
    ScriptNotReady,
    TimingsDoNotFit,
    VoiceUnavailable,
    apply_take_timings,
    generate_voice_over,
    get_short_script,
    latest_take,
    list_voices,
    write_short_script,
)
from sreditor.srstudio_operations import NotInLibrary, all_entries
from sreditor.voice_generation import Take
from sreditor.voice_markup import TAGS, Tag, TagClass, TagForm, TtsModel, ValueKind
from sreditor.web.forms import CSRF_FIELD
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import revision_of
from sreditor.web.workspace.media import media_url
from sreditor.web.workspace.script_form import (
    BLOCK_PREFIX,
    SHOWN_THROUGHOUT,
    SHOWN_TIMED,
    TEXT_PREFIX,
    Errors,
    parse_script,
)
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    View,
    channel_page,
    done,
    require,
)

router = APIRouter()

PAGE = "/app/channels/{slug}/episodes/{number}/short-script"
SHORT_SCRIPT_SUFFIX = "/short-script"
SCRIPT_CHANGED = N_(
    "Someone changed the short script after you opened the page. Reload the page to see their "
    "changes, then try again."
)
TIMINGS_STALE = N_("The script's blocks changed since this take. Generate the voice-over again.")
GENERATION_FAILED = N_("ElevenLabs could not generate the voice-over: %(reason)s")
VOICES_FAILED = N_("ElevenLabs did not list the voices, so type the voice id instead.")

FUNCTION_LABELS: dict[BlockFunction, str] = {
    BlockFunction.HOOK: N_("Hook"),
    BlockFunction.DEVELOPMENT: N_("Development"),
    BlockFunction.TURN: N_("Turn"),
    BlockFunction.PUNCH: N_("Punch"),
    BlockFunction.CTA: N_("Call to action"),
}
REACTION_LABELS: dict[Reaction, str] = {
    Reaction.CURIOSITY: N_("Curiosity"),
    Reaction.RECOGNITION: N_("Recognition"),
    Reaction.TENSION: N_("Tension"),
    Reaction.SURPRISE: N_("Surprise"),
    Reaction.OUTRAGE: N_("Outrage"),
    Reaction.LAUGHTER: N_("Laughter"),
    Reaction.DISCOMFORT: N_("Discomfort"),
}
MODEL_LABELS: dict[TtsModel, str] = {
    TtsModel.V3: N_("Eleven v3: audio tags, emotion and reactions"),
    TtsModel.V2: N_("Multilingual v2: timed pauses only"),
}
CLASS_LABELS: dict[TagClass, str] = {
    TagClass.RHYTHM: N_("Rhythm"),
    TagClass.VOICE: N_("Voice"),
    TagClass.EMOTION: N_("Emotion"),
    TagClass.REACTION: N_("Reaction"),
    TagClass.SOUND: N_("Sound, for the edit"),
    TagClass.VISUAL: N_("Camera, for the edit"),
    TagClass.PASSTHROUGH: N_("Any other v3 tag"),
}
VALUE_SAMPLES = {ValueKind.SECONDS: ":0.5", ValueKind.NAME: ":…"}
NO_REACTION = N_("None")
NARRATOR = N_("The narrator")
NOT_IN_LIBRARY = N_(
    "This character or prop is no longer in the library. Pick another and save again."
)
BEATS: TypeAdapter[list[Beat]] = TypeAdapter(list[Beat])
CHOOSE_VOICE = N_("Choose a voice")
BLOCK_NUMBER = N_("Block %(number)s")


class ScriptView(BaseModel):
    """The short script page, with what was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    number: int
    outcome: Outcome = Outcome()
    sent: dict[str, str] | None = None


def short_script_url(slug: str, number: int) -> str:
    """The short script page of an episode."""
    return episodes_url(slug, number) + SHORT_SCRIPT_SUFFIX


def options(labels_by_value: dict[Any, str], translate: Translator) -> list[tuple[str, str]]:
    """(value, label) pairs for a select, translated."""
    return [(str(value), translate(label)) for value, label in labels_by_value.items()]


def warning_text(found: ScriptWarning, translate: Translator) -> str:
    """A script warning in the reader's language."""
    return translate(WARNING_MESSAGES[found.kind], **found.values)


def tag_guide(translate: Translator) -> list[tuple[str, list[tuple[str, str]]]]:
    """The voice tags by class: how to write each one and what it does."""
    guide: list[tuple[str, list[tuple[str, str]]]] = []
    for tag_class, class_label in CLASS_LABELS.items():
        entries = [
            (example_of(tag), translate(tag.effect)) for tag in TAGS if tag.tag_class == tag_class
        ]
        guide.append((translate(class_label), entries))
    return guide


def example_of(tag: Tag) -> str:
    """How a tag is written, such as [pause:0.5] or [irony]…[/irony]."""
    sample = VALUE_SAMPLES.get(tag.value, "")
    opening = f"[{tag.name}{sample}]"
    return f"{opening}…[/{tag.name}]" if tag.form == TagForm.SPAN else opening


def block_rows(script: ShortScript, sent: dict[str, str] | None) -> list[dict[str, str]]:
    """Each block as form values, plus a spare row for a new one."""
    if sent is not None:
        return sent_rows(sent, BLOCK_PREFIX)
    rows = [
        {
            "start_s": f"{block.start_s:g}",
            "end_s": f"{block.end_s:g}",
            "function": block.function.value,
            "speech": block.speech,
            "reaction": block.impact.reaction.value if block.impact else "",
            "intensity": str(block.impact.intensity) if block.impact else "",
            "note": block.impact.note if block.impact else "",
            "speaker": str(block.speaker or ""),
            "beats": BEATS.dump_json(block.beats).decode() if block.beats else "",
        }
        for block in script.blocks
    ]
    last_end = f"{script.blocks[-1].end_s:g}" if script.blocks else "0"
    return [*rows, {"start_s": last_end, "function": BlockFunction.HOOK.value if not rows else ""}]


def text_rows(script: ShortScript, sent: dict[str, str] | None) -> list[dict[str, str]]:
    """Each on-screen text as form values, plus a spare row for a new one."""
    if sent is not None:
        return sent_rows(sent, TEXT_PREFIX)
    rows = [
        {
            "text": item.text,
            "start_s": "" if item.start_s is None else f"{item.start_s:g}",
            "end_s": "" if item.end_s is None else f"{item.end_s:g}",
            "shown": SHOWN_THROUGHOUT if item.is_persistent else SHOWN_TIMED,
        }
        for item in script.screen_texts
    ]
    return [*rows, {}]


def sent_rows(sent: dict[str, str], prefix: str) -> list[dict[str, str]]:
    """The rows a failed form posted, so nobody loses what they typed."""
    count = int(sent.get(f"{prefix}_count", "0") or 0)
    marker = f"{prefix}-"
    rows: list[dict[str, str]] = [{} for _ in range(count)]
    for name, value in sent.items():
        if name.startswith(marker):
            index, _, field = name[len(marker) :].partition("-")
            if index.isdigit() and int(index) < count:
                rows[int(index)][field] = value
    return rows


def voice_choices(app: Services, access: ChannelAccess) -> tuple[list[Voice] | None, str]:
    """The voices to pick from, or None with a note when they cannot be listed."""
    if app.voice is None or not access.can(Permission.GENERATE_VOICE):
        return None, ""
    try:
        return list_voices(app, access), ""
    except ElevenLabsError:
        return None, VOICES_FAILED


def take_context(
    app: Services, access: ChannelAccess, number: int, translate: Translator
) -> dict[str, Any]:
    """The latest take, where to play it and the times it measured for each block."""
    take: Take | None = latest_take(app, access, number)
    if take is None:
        return {"take": None, "take_file": "", "take_times": []}
    times = [
        (translate(BLOCK_NUMBER, number=index), f"{planned.start_s:g}–{planned.end_s:g} s")
        for index, planned in enumerate(take.suggested or [], start=1)
    ]
    file_link = f"{media_url(access.channel.slug, number)}/{take.media_id}/file"
    return {"take": take, "take_file": file_link, "take_times": times}


def voice_options(voices: list[Voice] | None, translate: Translator) -> list[tuple[str, str]]:
    """The voices as select options, after a prompt to choose one."""
    listed = [(voice.voice_id, voice.name) for voice in voices or []]
    return [("", translate(CHOOSE_VOICE)), *listed]


def form_context(script: ShortScript, shown: ScriptView, translate: Translator) -> dict[str, Any]:
    """The script's fields as typed or stored, with the choices each select offers."""
    sent = shown.sent
    return {
        "duration": sent.get("duration_s", "") if sent else f"{script.duration_s:g}",
        "voice_values": {
            "model": sent.get("model", "") if sent else script.voice.model.value,
            "voice_id": sent.get("voice_id", "") if sent else script.voice.voice_id,
            "language": sent.get("language", "") if sent else script.voice.language,
        },
        "blocks": block_rows(script, sent),
        "texts": text_rows(script, sent),
        "function_options": options(FUNCTION_LABELS, translate),
        "language_options": list(SPEECH_LANGUAGES.items()),
        "reaction_options": [("", translate(NO_REACTION)), *options(REACTION_LABELS, translate)],
        "model_options": options(MODEL_LABELS, translate),
        "shown_options": [
            (SHOWN_TIMED, translate(N_("At its times"))),
            (SHOWN_THROUGHOUT, translate(N_("The whole video"))),
        ],
        "intensity_options": [("", "")]
        + [(str(level), str(level)) for level in range(MIN_INTENSITY, MAX_INTENSITY + 1)],
    }


def speaker_options(
    app: Services, access: ChannelAccess, translate: Translator
) -> list[tuple[str, str]]:
    """Who may speak a block: the narrator voice or a character of the channel's library."""
    characters = all_entries(app, access.channel.slug, LibraryKind.CHARACTER)
    return [("", translate(NARRATOR)), *((str(entry.id), entry.name) for entry in characters)]


def library_error(sent: dict[str, str], missing: NotInLibrary, translate: Translator) -> Errors:
    """The refusal next to the block it names: the n-th block with speech is that row."""
    filled = [
        index
        for index in range(int(sent.get(f"{BLOCK_PREFIX}_count", "0") or 0))
        if sent.get(f"{BLOCK_PREFIX}-{index}-speech", "").strip()
    ]
    block = int(missing.location[1])
    field = "speaker" if missing.location[-1] == "speaker" else "speech"
    row = filled[block] if block < len(filled) else 0
    return {f"{BLOCK_PREFIX}-{row}-{field}": translate(NOT_IN_LIBRARY)}


def short_script_page(
    request: Request, app: Services, access: ChannelAccess, shown: ScriptView
) -> Response:
    """The short script form, its warnings, the voice-over panel and the tag guide."""
    translate = translator(request)
    current = found_episode(app, access, shown.number)
    script = get_short_script(app, access, shown.number)
    voices, voices_note = voice_choices(app, access)
    context = {
        "episode": current.episode,
        "script": script,
        "warnings": [warning_text(found, translate) for found in script.warnings],
        "errors": shown.outcome.errors,
        "voices": voices,
        "voice_options": voice_options(voices, translate),
        "speaker_options": speaker_options(app, access, translate),
        "function_labels": FUNCTION_LABELS,
        "voices_note": translate(voices_note) if voices_note else "",
        "may_generate": app.voice is not None and access.can(Permission.GENERATE_VOICE),
        "script_link": short_script_url(access.channel.slug, shown.number),
        "tag_guide": tag_guide(translate),
        "speech_limit": SPEECH_LENGTH,
        "screen_text_limit": SCREEN_TEXT_LENGTH,
        "episode_tab": "short-script",
        **episode_frame(app, access, shown.number, translate),
        **form_context(script, shown, translate),
        **take_context(app, access, shown.number, translate),
        **labels(),
    }
    view = View(
        template="workspace/short_script.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PAGE)
def short_script(request: Request, app: ServicesDep, access: PageAccess, number: int) -> Response:
    """An episode's short script."""
    return short_script_page(request, app, access, ScriptView(number=number))


async def posted(request: Request) -> dict[str, str]:
    """The form's text fields."""
    form = await request.form()
    return {name: value for name, value in form.items() if isinstance(value, str)}


@router.post(PAGE)
async def save_short_script(
    request: Request, app: ServicesDep, access: PageAccess, number: int
) -> Response:
    """Save the whole short script."""
    sent = await posted(request)
    if rejected := rejected_form(request, app, sent.get(CSRF_FIELD, "")):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    return saved_or_shown(request, app, access, ScriptView(number=number, sent=sent))


def saved_or_shown(
    request: Request, app: Services, access: ChannelAccess, shown: ScriptView
) -> Response:
    """Write the script and go back to it, or show the form again with its errors."""
    sent = shown.sent or {}
    parsed = parse_script(sent, translator(request))
    errors: Errors = parsed.errors
    if parsed.script is not None:
        try:
            write_short_script(
                app, access, shown.number, parsed.script, revision_of(sent.get("revision", ""))
            )
        except PartChanged:
            errors = {"form": translator(request)(SCRIPT_CHANGED)}
        except NotInLibrary as missing:
            errors = library_error(sent, missing, translator(request))
        else:
            return done(
                request, app, short_script_url(access.channel.slug, shown.number), Flash.SAVED
            )
    outcome = Outcome.conflict(errors) if "form" in errors else Outcome.invalid(errors)
    return short_script_page(request, app, access, shown.model_copy(update={"outcome": outcome}))


@router.post(PAGE + "/voice-over")
async def generate(request: Request, app: ServicesDep, access: PageAccess, number: int) -> Response:
    """Generate a voice-over take of the script as saved."""
    sent = await posted(request)
    if rejected := rejected_form(request, app, sent.get(CSRF_FIELD, "")):
        return rejected
    require(access, Permission.GENERATE_VOICE)
    translate = translator(request)
    try:
        take = generate_voice_over(app, access, number)
    except (ScriptNotReady, VoiceUnavailable) as refused:
        message = translate(str(refused))
    except ElevenLabsError as failure:
        message = translate(GENERATION_FAILED, reason=str(failure))
    else:
        flash = Flash.VOICE_REUSED if take.is_reused else Flash.VOICE_GENERATED
        return done(request, app, short_script_url(access.channel.slug, number), flash)
    shown = ScriptView(number=number, outcome=Outcome.conflict({"voice": message}))
    return short_script_page(request, app, access, shown)


@router.post(PAGE + "/timings")
async def use_timings(
    request: Request, app: ServicesDep, access: PageAccess, number: int
) -> Response:
    """Replace the planned times with those of the latest take."""
    sent = await posted(request)
    if rejected := rejected_form(request, app, sent.get(CSRF_FIELD, "")):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    translate = translator(request)
    try:
        apply_take_timings(
            app, access, number, sent.get("take_key", ""), revision_of(sent.get("revision", ""))
        )
    except TimingsDoNotFit:
        message = translate(TIMINGS_STALE)
    except PartChanged:
        message = translate(SCRIPT_CHANGED)
    else:
        return done(
            request, app, short_script_url(access.channel.slug, number), Flash.TIMINGS_APPLIED
        )
    shown = ScriptView(number=number, outcome=Outcome.conflict({"voice": message}))
    return short_script_page(request, app, access, shown)
