"""An episode as the zip srstudio reads unchanged, or the list of what it still lacks (rule project.srstudio).

Owners and editors export. The zip holds timeline.json and a WAV per take (audio/), the channel bible
(bible/cast.json, bible/relationships.json), the beats (assets/poses.json, assets/events.json) when
the episode's style draws them, style.json with the visual style as data (rule project.visual-style)
and episode.json with its slug, seed and frame. Every piece is
checked first; when one is missing the export lists them all and writes nothing, so srstudio never
receives a broken file. Exports only read: no waivers, no video, only the takes' audio (project.export).
"""

from __future__ import annotations

import json
import zipfile
from importlib import metadata
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from sreditor.assets import AssetMissing
from sreditor.block_takes import BlockTake, BlockTakeRecord
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import episode_access
from sreditor.i18n import N_
from sreditor.library import LibraryEntry, LibraryKind
from sreditor.members import Permission
from sreditor.short_script import ShortScript
from sreditor.short_script_operations import ScriptNotReady, block_plans
from sreditor.srstudio import CharacterSheet, EntrySheet, PlaceSheet, StyleSheet
from sreditor.srstudio_bible import BibleSource, cast_document, relationships_document
from sreditor.srstudio_locks import lock_exported_slugs
from sreditor.srstudio_operations import all_entries, published_numbers
from sreditor.srstudio_timeline import (
    TimedBlock,
    events_document,
    poses_document,
    timeline_document,
    wav_take,
)
from sreditor.srstudio_vocabulary import (
    BEAT_STYLES,
    FRAMES,
    SRSTUDIO_REVISION,
    Style,
    toon_character,
)
from sreditor.voice_generation import take_key

NARRATOR = "narrator"
BIBLE_VERSION = "1.0.0"
PACKAGE = "sreditor"
JSON_INDENT = 2

NO_SCRIPT = N_("The episode has no short script with spoken blocks.")
NO_TAKE = N_("Generate the voice-over of the short script, one take per block.")
STALE_TAKE = N_("The short script changed after the last voice-over. Generate it again.")
NOT_READY = N_("The voice-over cannot be generated yet: %(reason)s")
TAKE_GONE = N_("The take of block %(block)s is gone. Generate the voice-over again.")
NO_WAV = N_("The take of block %(block)s could not be turned into WAV; the server needs ffmpeg.")
NO_WORLD = N_("%(name)s has no srstudio world.")
NO_KIND = N_("Character %(name)s has no kind (person, animal, object or archetype).")
UNPUBLISHED = N_("A fact about %(name)s cites episode %(number)s, which is no longer published.")
NOT_TOON = N_("%(name)s has beats, but their rig is not a toon character such as toon-dog.")
NO_STYLE = N_("Pick the episode's visual style, with its srstudio style, before exporting.")


class Missing(BaseModel):
    """One piece the export lacks: a message to translate and its values."""

    model_config = ConfigDict(frozen=True)

    message: str
    values: dict[str, str] = Field(default_factory=dict)

    def english(self) -> str:
        """The message in English, for the API."""
        return self.message % self.values if self.values else self.message


class SrstudioNotReady(ValueError):
    """The episode lacks pieces srstudio needs; nothing was exported."""

    def __init__(self, missing: list[Missing]) -> None:
        super().__init__("; ".join(item.english() for item in missing))
        self.missing = missing


class Gathered(BaseModel):
    """What the export reads before writing anything, and what it found missing."""

    model_config = ConfigDict(frozen=True)

    script: ShortScript
    takes: list[BlockTake | None]
    missing: list[Missing]


class Library(BaseModel):
    """The channel's characters and places with their sheets."""

    model_config = ConfigDict(frozen=True)

    characters: list[tuple[LibraryEntry, EntrySheet]]
    places: list[tuple[LibraryEntry, EntrySheet]]


def generated_by() -> str:
    """Who wrote the files, as srstudio's bible asks: the tool and its version."""
    return f"{PACKAGE} {metadata.version(PACKAGE)} (srstudio {SRSTUDIO_REVISION})"


def has_audio(app: Services, take: BlockTake) -> bool:
    """Whether a block take's audio is still on disk."""
    try:
        app.assets.path_of(take.file)
    except AssetMissing:
        return False
    return True


def block_takes(
    app: Services, script: ShortScript, keys: list[str | None]
) -> tuple[list[BlockTake | None], list[Missing]]:
    """The block take of each spoken block, and the ones whose audio is gone."""
    takes: list[BlockTake | None] = []
    missing: list[Missing] = []
    for index, key in enumerate(keys):
        record = BlockTakeRecord(channel=script.channel, number=script.number, key=key or "")
        found = app.block_takes.find(record) if key else None
        if key and (found is None or not has_audio(app, found)):
            missing.append(Missing(message=TAKE_GONE, values={"block": str(index + 1)}))
            found = None
        takes.append(found)
    return takes, missing


def gather_takes(app: Services, script: ShortScript) -> Gathered:
    """The script and the takes of its latest voice-over, if it matches the script as it is now."""

    def lacking(message: str, **values: str) -> Gathered:
        return Gathered(script=script, takes=[], missing=[Missing(message=message, values=values)])

    if not script.blocks:
        return lacking(NO_SCRIPT)
    try:
        plans = block_plans(app, script.channel, script)
    except ScriptNotReady as refused:
        return lacking(NOT_READY, reason=str(refused))
    keys = [plan.key if plan else None for plan in plans]
    latest = app.takes.latest(script.channel, script.number)
    if latest is None or latest.blocks is None:
        return lacking(NO_TAKE)
    if latest.key != take_key(keys):
        return lacking(STALE_TAKE)
    takes, missing = block_takes(app, script, keys)
    return Gathered(script=script, takes=takes, missing=missing)


def channel_library(app: Services, channel: str) -> Library:
    """Every character and place of the channel with its sheet."""
    characters = all_entries(app, channel, LibraryKind.CHARACTER)
    places = all_entries(app, channel, LibraryKind.LOCATION)
    sheets = app.srstudio.sheets(characters + places)
    return Library(
        characters=[(entry, sheets[entry.id]) for entry in characters],
        places=[(entry, sheets[entry.id]) for entry in places],
    )


def sheet_gaps(
    entry: LibraryEntry, sheet: CharacterSheet | PlaceSheet, published: set[int]
) -> list[Missing]:
    """What keeps one bible entry from loading in srstudio."""
    name = {"name": entry.name}
    gaps = [] if sheet.world else [Missing(message=NO_WORLD, values=name)]
    if isinstance(sheet, CharacterSheet) and sheet.character_kind is None:
        gaps.append(Missing(message=NO_KIND, values=name))
    facts = sheet.canon if isinstance(sheet, CharacterSheet) else sheet.cites
    gaps += [
        Missing(message=UNPUBLISHED, values={**name, "number": str(number)})
        for number in sorted({fact.episode for fact in facts} - published)
    ]
    return gaps


def library_gaps(app: Services, channel: str, library: Library) -> list[Missing]:
    """What keeps the channel's bible from loading in srstudio."""
    published = published_numbers(app, channel)
    gaps: list[Missing] = []
    for entry, stored in library.characters + library.places:
        sheet = stored.sheet
        if isinstance(sheet, CharacterSheet | PlaceSheet):
            gaps += sheet_gaps(entry, sheet, published)
    return gaps


def reel_slugs(app: Services, channel: str) -> dict[int, str]:
    """The srstudio slug of each published episode, the reels facts cite."""
    slugs: dict[int, str] = {}
    for number in sorted(published_numbers(app, channel)):
        episode = app.episodes.get(channel, number).episode
        slugs[number] = app.srstudio.episode_slug(channel, number, episode.title).slug
    return slugs


def bible_source(app: Services, channel: str, library: Library) -> BibleSource:
    """The bible's entries, pairs and reels, ready to write."""
    return BibleSource(
        characters=[
            (entry, stored.slug, stored.sheet)
            for entry, stored in library.characters
            if isinstance(stored.sheet, CharacterSheet)
        ],
        places=[
            (entry, stored.slug, stored.sheet)
            for entry, stored in library.places
            if isinstance(stored.sheet, PlaceSheet)
        ],
        relationships=app.srstudio.relationships(channel),
        reels=reel_slugs(app, channel),
        version=BIBLE_VERSION,
        generated_by=generated_by(),
    )


def episode_style_sheet(app: Services, channel: str, number: int) -> tuple[str, StyleSheet] | None:
    """The slug and sheet of the visual style the episode picked, if it picked one with a sheet."""
    picked = app.library.episode_style(channel, number).style
    if picked is None:
        return None
    stored = app.srstudio.sheet(picked)
    return (stored.slug, stored.sheet) if isinstance(stored.sheet, StyleSheet) else None


def style_document(slug: str, sheet: StyleSheet) -> dict[str, Any]:
    """style.json: the visual style as data, so every episode of the channel looks alike.

    Unset parts are null, so srstudio can tell "no rule" from a missing file.
    """
    return {
        "slug": slug,
        "style": sheet.style.value if sheet.style else None,
        **sheet.model_dump(
            mode="json", include={"palette", "fonts", "cut_rhythm", "min_text_px", "safe_zone"}
        ),
    }


def toon_cast(script: ShortScript, library: Library) -> tuple[dict[str, str], list[Missing]]:
    """The toon each beat character is drawn as, and the characters whose rig draws no toon."""
    sheets = {entry.id: (entry, stored) for entry, stored in library.characters}
    cast: dict[str, str] = {}
    gaps: list[Missing] = []
    for character in sorted(
        {beat.character for block in script.blocks for beat in block.beats}, key=str
    ):
        entry, stored = sheets[character]
        rig = stored.sheet.rig if isinstance(stored.sheet, CharacterSheet) else ""
        toon = toon_character(rig)
        if toon is None:
            gaps.append(Missing(message=NOT_TOON, values={"name": entry.name}))
        else:
            cast[str(character)] = toon
    return cast, gaps


def actor_of(speaker: UUID | None, library: Library) -> str:
    """Who says a take in srstudio: the character's slug, or the narrator."""
    slugs = {entry.id: stored.slug for entry, stored in library.characters}
    return NARRATOR if speaker is None else slugs[speaker]


def timed_blocks(
    gathered: Gathered, library: Library, folder: Path, app: Services
) -> tuple[list[TimedBlock], list[Missing]]:
    """Each spoken block with its WAV take, back to back from 0."""
    timed: list[TimedBlock] = []
    missing: list[Missing] = []
    start = 0.0
    for index, (block, take) in enumerate(zip(gathered.script.blocks, gathered.takes, strict=True)):
        if take is None:
            continue
        wav = wav_take(len(timed) + 1, app.assets.path_of(take.file), folder)
        if wav is None:
            missing.append(Missing(message=NO_WAV, values={"block": str(index + 1)}))
            continue
        actor = actor_of(block.speaker, library)
        timed.append(
            TimedBlock(index=len(timed), block=block, actor=actor, take=take, wav=wav, start=start)
        )
        start += wav.duration_s
    return timed, missing


class Bundle(BaseModel):
    """Everything the zip holds: JSON documents by path and the WAV takes."""

    model_config = ConfigDict(frozen=True)

    documents: dict[str, Any]
    timed: list[TimedBlock]


def beat_documents(
    script: ShortScript, style: Style | None, library: Library
) -> tuple[list[Missing], bool]:
    """Whether beats are exported (only for a style that draws them) and what keeps them from it."""
    has_beats = any(block.beats for block in script.blocks)
    if not has_beats or style not in BEAT_STYLES:
        return [], False
    return toon_cast(script, library)[1], True


def episode_document(app: Services, script: ShortScript, style: Style) -> dict[str, Any]:
    """episode.json: the episode's slug, seed and frame, so a rebuild draws the same frames."""
    episode = app.episodes.get(script.channel, script.number).episode
    slug = app.srstudio.episode_slug(script.channel, script.number, episode.title)
    width, height, fps = FRAMES[style]
    return {
        "slug": slug.slug,
        "title": episode.title,
        "seed": slug.seed,
        "style": style.value,
        "width": width,
        "height": height,
        "fps": fps,
        "language": script.voice.language,
        "generated_by": generated_by(),
    }


class Checked(BaseModel):
    """What the export read and every piece it found missing, before any audio is converted."""

    model_config = ConfigDict(frozen=True)

    gathered: Gathered
    library: Library
    style: Style | None
    style_file: dict[str, Any] | None
    has_beats: bool
    missing: list[Missing]


def checked(app: Services, channel: str, number: int) -> Checked:
    """Read the script, takes, library and style, and list what srstudio would lack."""
    gathered = gather_takes(app, app.short_scripts.short_script(channel, number))
    library = channel_library(app, channel)
    stored_style = episode_style_sheet(app, channel, number)
    style = stored_style[1].style if stored_style else None
    beat_gaps, has_beats = beat_documents(gathered.script, style, library)
    missing = [*gathered.missing, *library_gaps(app, channel, library), *beat_gaps]
    missing += [] if style else [Missing(message=NO_STYLE)]
    return Checked(
        gathered=gathered,
        library=library,
        style=style,
        style_file=style_document(*stored_style) if stored_style else None,
        has_beats=has_beats,
        missing=missing,
    )


def missing_pieces(app: Services, access: ChannelAccess, number: int) -> list[Missing]:
    """What keeps the episode from exporting to srstudio; empty when it is ready (owners and editors)."""
    channel = episode_access(app, access, number, Permission.WRITE_CHANNEL)
    return checked(app, channel, number).missing


def bundle(app: Services, access: ChannelAccess, number: int, folder: Path) -> Bundle:
    """Check every piece, convert the takes and build the documents; SrstudioNotReady lists what is missing."""
    channel = episode_access(app, access, number, Permission.WRITE_CHANNEL)
    found = checked(app, channel, number)
    gathered, library, style, has_beats = (
        found.gathered, found.library, found.style, found.has_beats
    )  # fmt: skip
    if found.missing or style is None:
        raise SrstudioNotReady(found.missing)
    timed, wav_gaps = timed_blocks(gathered, library, folder, app)
    if wav_gaps:
        raise SrstudioNotReady(wav_gaps)
    source = bible_source(app, channel, library)
    timeline = timeline_document(timed, gathered.script.voice.model)
    documents: dict[str, Any] = {
        "episode.json": episode_document(app, gathered.script, style),
        "timeline.json": timeline,
        "bible/cast.json": cast_document(source),
        "bible/relationships.json": relationships_document(source),
    }
    if found.style_file is not None:
        documents["style.json"] = found.style_file
    if has_beats:
        cast = toon_cast(gathered.script, library)[0]
        documents["assets/poses.json"] = poses_document(timed, cast, timeline["duration"])
        documents["assets/events.json"] = events_document(timed)
    return Bundle(documents=documents, timed=timed)


def write_zip(found: Bundle, target: Path) -> Path:
    """Write the bundle as a zip; each WAV is copied from its file, never held whole in memory."""
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, document in found.documents.items():
            archive.writestr(
                name, json.dumps(document, ensure_ascii=False, indent=JSON_INDENT) + "\n"
            )
        for timed in found.timed:
            archive.write(timed.wav.path, timed.wav.name, compress_type=zipfile.ZIP_STORED)
    return target


def srstudio_zip(app: Services, access: ChannelAccess, number: int, folder: Path) -> Path:
    """The episode's srstudio zip, written in `folder`; SrstudioNotReady lists what is missing."""
    found = bundle(app, access, number, folder)
    slug = found.documents["episode.json"]["slug"]
    archive = write_zip(found, folder / f"{slug}.zip")
    lock_exported_slugs(app, access.channel.slug, number)
    return archive
