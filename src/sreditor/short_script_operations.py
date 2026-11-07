"""Short script and voice-over operations shared by the API, the MCP tools and the pages.

Writing the short script is content work, open to contributors and the content:write scope.
Generating a voice-over spends the account's ElevenLabs credits, so only owners and editors
may (rules project.voice-generation, agentic.gate); asking again for the same speech reuses the
earlier take for free.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.assets import AssetMissing
from sreditor.audio_finish import finish
from sreditor.block_takes import (
    BlockTake,
    BlockTakeRecord,
    BlockVoice,
    VoiceLicense,
    audio_seconds,
    block_take_key,
    words_of,
)
from sreditor.channels import NarratorVoice
from sreditor.deps import ChannelAccess, Services
from sreditor.elevenlabs import MODEL_IDS, PROVIDER, ElevenLabs, SpeechRequest, Voice
from sreditor.episode_operations import episode_access
from sreditor.i18n import N_
from sreditor.library import LibraryKind
from sreditor.media import KIND_FORMATS, MediaInput, MediaKind, MediaNotFound, NewMedia
from sreditor.members import Permission
from sreditor.operations import gated
from sreditor.short_script import ShortScript, ShortScriptInput, speech_language_for
from sreditor.srstudio import CharacterSheet
from sreditor.srstudio_operations import check_cast
from sreditor.voice_generation import (
    Take,
    TakeRecord,
    joined_timings,
    language_code,
    spoken_blocks,
    suggested_times,
    take_key,
)
from sreditor.voice_report import VoiceOverTake, voice_over_of
from sreditor.voice_tuning import VoiceTuning


class BlockPlan(BaseModel):
    """What one block asks ElevenLabs for: the text as the model reads it, and the voice."""

    model_config = ConfigDict(frozen=True)

    text: str
    voice: BlockVoice
    key: str


class ScriptOperation:
    """stmt operation names an agent's short script and voice writes are gated on."""

    WRITE = "short_script.write"
    GENERATE = "voice.generate"


class VoiceUnavailable(RuntimeError):
    """No ElevenLabs key is set, so voice-overs cannot be generated."""


class ScriptNotReady(ValueError):
    """The short script lacks what a voice-over needs."""


class VoiceOverNotMade(LookupError):
    """The episode has no voice-over yet."""


class TimingsDoNotFit(ValueError):
    """The take was made from a script with a different number of blocks."""


NO_VOICE = N_("Choose a voice for the short script first.")
NO_VOICE_OVER = "This episode has no voice-over yet."
NO_SPEECH = N_("Write at least one block with speech first.")
NO_CHARACTER_VOICE = N_(
    "A character who speaks has no ElevenLabs voice yet. Choose one on their srstudio sheet."
)
BLOCKS_CHANGED = (
    "The take was made when the script had %(take)s blocks; it has %(now)s now. Generate it again."
)
NO_TIMESTAMPS = "ElevenLabs gave no timestamps for this take, so its times are unknown."
GENERATION_OFF = N_("Voice-over generation is off: SREDITOR_ELEVENLABS_API_KEY is not set.")


def get_short_script(app: Services, access: ChannelAccess, number: int) -> ShortScript:
    """The episode's short script, with its warnings and revision.

    One nobody wrote yet speaks the channel's language, so a channel in English does not start
    from Portuguese.
    """
    slug = episode_access(app, access, number, Permission.READ)
    script = app.short_scripts.short_script(slug, number)
    if script.revision:
        return script
    language = speech_language_for(access.channel.language)
    voice = script.voice.model_copy(update={"language": language})
    return script.model_copy(update={"voice": voice})


def in_channel_language(data: ShortScriptInput, channel_language: str) -> ShortScriptInput:
    """The script, speaking the channel's language when the caller named none."""
    if "voice" in data.model_fields_set and "language" in data.voice.model_fields_set:
        return data
    voice = data.voice.model_copy(update={"language": speech_language_for(channel_language)})
    return data.model_copy(update={"voice": voice})


def write_short_script(
    app: Services,
    access: ChannelAccess,
    number: int,
    data: ShortScriptInput,
    expected_revision: int | None,
) -> ShortScript:
    """Replace the episode's short script; without a language it speaks the channel's."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    written = in_channel_language(data, access.channel.language)
    check_cast(app, slug, written.blocks)
    return gated(
        app,
        access,
        ScriptOperation.WRITE,
        lambda: app.short_scripts.write(slug, number, written, expected_revision),
    )


def voice_service(app: Services) -> ElevenLabs:
    """The ElevenLabs client; VoiceUnavailable when no key is set."""
    if app.voice is None:
        raise VoiceUnavailable(GENERATION_OFF)
    return app.voice


def list_voices(app: Services, access: ChannelAccess) -> list[Voice]:
    """The ElevenLabs voices a short script can use."""
    access.require(Permission.GENERATE_VOICE)
    return voice_service(app).voices()


def is_still_there(app: Services, record: TakeRecord, take: Take) -> bool:
    """Whether the take's media file is still part of the episode."""
    try:
        app.media.get(record.channel, record.number, take.media_id)
    except MediaNotFound:
        return False
    return True


def has_audio(app: Services, take: BlockTake) -> bool:
    """Whether a block take's audio is still in the assets directory."""
    try:
        app.assets.path_of(take.file)
    except AssetMissing:
        return False
    return True


def narrator_voice(script: ShortScriptInput, default: NarratorVoice | None) -> BlockVoice:
    """The episode's narrator: the script's own voice, else the brand's default narrator.

    The script's voice keeps the voice's own settings; the brand's brings its name and tuning
    (rule project.voice.estimate).
    """
    chosen = script.voice
    if chosen.voice_id:
        return BlockVoice(
            voice_id=chosen.voice_id, voice_name="", model=chosen.model, language=chosen.language
        )
    if default is None:
        raise ScriptNotReady(NO_VOICE)
    return BlockVoice(
        voice_id=default.voice_id,
        voice_name=default.voice_name,
        model=chosen.model,
        language=chosen.language,
        tuning=default.tuning,
    )


def character_voice(
    app: Services, channel: str, speaker: UUID, script: ShortScriptInput
) -> BlockVoice:
    """A library character's voice and tuning, from their srstudio sheet (rule project.srstudio.voice)."""
    entry = app.library.get(channel, LibraryKind.CHARACTER, speaker)
    sheet = app.srstudio.sheet(entry).sheet
    if not isinstance(sheet, CharacterSheet) or not sheet.voice.voice_id:
        raise ScriptNotReady(NO_CHARACTER_VOICE)
    voice = sheet.voice
    tuning = VoiceTuning(
        stability=voice.stability,
        similarity_boost=voice.similarity_boost,
        style=voice.style,
        speed=voice.speed,
        use_speaker_boost=voice.use_speaker_boost,
    )
    return BlockVoice(
        voice_id=voice.voice_id,
        voice_name=voice.voice_name or entry.name,
        model=script.voice.model,
        language=script.voice.language,
        tuning=tuning,
    )


def block_plans(app: Services, channel: str, script: ShortScriptInput) -> list[BlockPlan | None]:
    """What each block asks ElevenLabs for, None for a block without speech."""
    default = app.channels.get(channel).brand.narrator_voice
    plans: list[BlockPlan | None] = []
    for block, text in zip(script.blocks, spoken_blocks(script), strict=True):
        if not text.strip():
            plans.append(None)
            continue
        if block.speaker is None:
            voice = narrator_voice(script, default)
        else:
            voice = character_voice(app, channel, block.speaker, script)
        plans.append(BlockPlan(text=text, voice=voice, key=block_take_key(text, voice)))
    return plans


def spoken_take(app: Services, record: TakeRecord, plan: BlockPlan) -> BlockTake:
    """A block's take: the earlier one for the same text and voice, else a new one from ElevenLabs."""
    block_record = BlockTakeRecord(channel=record.channel, number=record.number, key=plan.key)
    earlier = app.block_takes.find(block_record)
    if earlier is not None and has_audio(app, earlier):
        return earlier
    voice = plan.voice
    speech = voice_service(app).speak(
        SpeechRequest(
            text=plan.text,
            voice_id=voice.voice_id,
            model=voice.model,
            language_code=language_code(voice.language),
            tuning=voice.tuning,
        )
    )
    accepted = KIND_FORMATS[MediaKind.VOICE_OVER]
    stored = app.assets.save_stream(
        record.channel, accepted, BytesIO(speech.audio), app.media.max_bytes
    )
    made_at = app.takes.now()
    take = BlockTake(
        key=plan.key,
        text=plan.text,
        voice=voice,
        file=stored,
        # The larger of the two: tags or a variable bitrate can make the size alone too short.
        duration_s=max(audio_seconds(len(speech.audio)), speech.duration_s),
        words=words_of(speech.alignment),
        created_at=made_at,
        license=voice_license(voice, voice_service(app).plan(), made_at),
    )
    app.block_takes.save(record.channel, record.number, take)
    return take


def voice_license(voice: BlockVoice, plan: str, made_at: datetime) -> VoiceLicense:
    """Where a take's voice came from: provider, voice, model and the account's plan."""
    return VoiceLicense(
        provider=PROVIDER,
        voice_id=voice.voice_id,
        voice_name=voice.voice_name,
        model=MODEL_IDS[voice.model],
        plan=plan,
        created_at=made_at,
    )


def named(app: Services, record: TakeRecord, take: BlockTake) -> BlockTake:
    """The take with its voice's name in the license, when it was made without one."""
    licensed = take.license
    if licensed is None or licensed.voice_name:
        return take
    name = voice_name(voice_service(app), licensed.voice_id)
    if name == licensed.voice_id:
        return take
    renamed = take.model_copy(update={"license": licensed.model_copy(update={"voice_name": name})})
    app.block_takes.save(record.channel, record.number, renamed)
    return renamed


def voice_names(app: Services, plans: list[BlockPlan]) -> str:
    """The voices a voice-over uses, by name, for the media list."""
    names: list[str] = []
    for plan in plans:
        name = plan.voice.voice_name or voice_name(voice_service(app), plan.voice.voice_id)
        if name not in names:
            names.append(name)
    return ", ".join(names)


def voice_name(voice: ElevenLabs, voice_id: str) -> str:
    """The voice's name for the media list, else its id."""
    return next((item.name for item in voice.voices() if item.voice_id == voice_id), voice_id)


def make_take(
    app: Services, record: TakeRecord, script: ShortScript, plans: list[BlockPlan | None]
) -> Take:
    """Take each block, join the takes into one voice-over file and remember what was measured."""
    made = [None if plan is None else spoken_take(app, record, plan) for plan in plans]
    spoken = [plan for plan in plans if plan is not None]
    takes = [None if take is None else named(app, record, take) for take in made]
    audio = b"".join(app.assets.path_of(take.file).read_bytes() for take in takes if take)
    chosen = script.voice
    names = voice_names(app, spoken)
    described = MediaInput(
        name=f"{names} · {chosen.model.value}",
        note=f"Generated with ElevenLabs {MODEL_IDS[chosen.model]}, one take per block, voices"
        f" {names}, language {chosen.language}.",
    )
    new = NewMedia(
        channel=record.channel, number=record.number, kind=MediaKind.VOICE_OVER, data=described
    )
    media = app.media.add(new, BytesIO(finish(audio) or audio))
    timings = joined_timings(takes)
    duration = round(sum(take.duration_s for take in takes if take), 3)
    take = Take(
        key=record.key,
        media_id=media.id,
        text=" ".join(plan.text for plan in spoken),
        model=chosen.model.value,
        voice_id=chosen.voice_id or spoken[0].voice.voice_id,
        language=chosen.language,
        duration_s=duration,
        timings=timings,
        suggested=suggested_times(timings, duration),
        created_at=app.takes.now(),
        blocks=[None if plan is None else plan.key for plan in plans],
    )
    app.takes.save(record.channel, record.number, take)
    return take


def stored_block_takes(app: Services, record: TakeRecord, take: Take) -> list[BlockTake | None]:
    """The block take of each block of a voice-over, None for a block without speech or take."""
    return [
        None
        if key is None
        else app.block_takes.find(
            BlockTakeRecord(channel=record.channel, number=record.number, key=key)
        )
        for key in take.blocks or []
    ]


def with_block_takes(app: Services, record: TakeRecord, take: Take) -> VoiceOverTake:
    """The voice-over with each spoken block's take and its words (rule project.voice.estimate)."""
    return voice_over_of(take, stored_block_takes(app, record, take))


def generate_voice_over(app: Services, access: ChannelAccess, number: int) -> VoiceOverTake:
    """A voice-over of the short script as it is now, one take per block in its speaker's voice.

    The same script reuses the earlier voice-over, and an unchanged block reuses its take, so
    only the blocks that changed spend credits: estimate_voice_over counts exactly those.
    """
    slug = episode_access(app, access, number, Permission.GENERATE_VOICE)
    script = app.short_scripts.short_script(slug, number)
    plans = block_plans(app, slug, script)
    if not any(plans):
        raise ScriptNotReady(NO_SPEECH)
    record = TakeRecord(
        channel=slug, number=number, key=take_key([plan.key if plan else None for plan in plans])
    )
    earlier = app.takes.find(record)
    if earlier is not None and is_still_there(app, record, earlier):
        return with_block_takes(app, record, earlier.model_copy(update={"is_reused": True}))
    voice_service(app)
    made = gated(
        app, access, ScriptOperation.GENERATE, lambda: make_take(app, record, script, plans)
    )
    return with_block_takes(app, record, made)


def get_voice_over(app: Services, access: ChannelAccess, number: int) -> VoiceOverTake:
    """The episode's latest voice-over with each block's take and words; VoiceOverNotMade if none."""
    take = latest_take(app, access, number)
    if take is None:
        raise VoiceOverNotMade(NO_VOICE_OVER)
    record = TakeRecord(channel=access.channel.slug, number=number, key=take.key)
    return with_block_takes(app, record, take)


def latest_take(app: Services, access: ChannelAccess, number: int) -> Take | None:
    """The episode's most recent take whose file is still there."""
    slug = episode_access(app, access, number, Permission.READ)
    take = app.takes.latest(slug, number)
    if take is None:
        return None
    record = TakeRecord(channel=slug, number=number, key=take.key)
    return take if is_still_there(app, record, take) else None


def timed_script(script: ShortScript, take: Take) -> ShortScriptInput:
    """The script with the take's measured times and the audio's length."""
    if not take.suggested:
        raise TimingsDoNotFit(NO_TIMESTAMPS)
    suggested = take.suggested
    if len(suggested) != len(script.blocks):
        raise TimingsDoNotFit(BLOCKS_CHANGED % {"take": len(suggested), "now": len(script.blocks)})
    blocks = [
        block.model_copy(update={"start_s": planned.start_s, "end_s": planned.end_s})
        for block, planned in zip(script.blocks, suggested, strict=True)
    ]
    return ShortScriptInput(
        duration_s=suggested[-1].end_s,
        voice=script.voice,
        blocks=blocks,
        screen_texts=script.screen_texts,
    )


def apply_take_timings(
    app: Services, access: ChannelAccess, number: int, key: str, expected_revision: int | None
) -> ShortScript:
    """Replace the planned block times with those the take measured."""
    slug = episode_access(app, access, number, Permission.WRITE_CONTENT)
    take = app.takes.get(TakeRecord(channel=slug, number=number, key=key))
    script = app.short_scripts.short_script(slug, number)
    timed = timed_script(script, take)
    return gated(
        app,
        access,
        ScriptOperation.WRITE,
        lambda: app.short_scripts.write(slug, number, timed, expected_revision),
    )
