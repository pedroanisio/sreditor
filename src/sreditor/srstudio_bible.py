"""srstudio's bible (cast.json, relationships.json) written from the channel's library sheets (rule project.srstudio.bible).

srstudio refuses an empty text, so a field nobody filled in is written as an open question
("open: …"), which srstudio accepts and lists as a gap. A fact cites a published episode by number
in sreditor and by its slug (srstudio's reel) here. What srstudio cannot accept at all, such as a
character without a world, is reported by the exporter as missing instead of written here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.library import LibraryEntry
from sreditor.srstudio import CharacterSheet, Fact, PlaceSheet, Relationship, VoiceProfile
from sreditor.srstudio_vocabulary import BIBLE_SCHEMA, OPEN_MARK

ELEVENLABS = "elevenlabs"
SETTINGS_TEXT = (
    "stability {stability:g}, similarity boost {similarity_boost:g}, style {style:g},"
    " speed {speed:g}, speaker boost {boost}"
)


class BibleSource(BaseModel):
    """What the bible is written from: entries with sheets, pairs and the reel slug of each episode."""

    model_config = ConfigDict(frozen=True)

    characters: list[tuple[LibraryEntry, str, CharacterSheet]]
    places: list[tuple[LibraryEntry, str, PlaceSheet]]
    relationships: list[Relationship]
    reels: dict[int, str]  # episode number -> episode slug
    version: str
    generated_by: str


def open_text(text: str, label: str) -> str:
    """The text, or an open question naming the field when nobody wrote it."""
    return text.strip() or f"{OPEN_MARK} {label}"


def fact_data(fact: Fact, reels: dict[int, str]) -> dict[str, str]:
    """A fact as srstudio cites it: by reel slug instead of episode number."""
    return {
        "fact": fact.fact,
        "field": fact.field.value,
        "reel": reels[fact.episode],
        "kind": fact.kind.value,
        "quote": fact.quote,
    }


def reels_of(facts: list[Fact], reels: dict[int, str]) -> list[str]:
    """The reels these facts cite, each once, in episode order."""
    return [reels[number] for number in sorted({fact.episode for fact in facts})]


def voice_data(voice: VoiceProfile) -> dict[str, str]:
    """A character's voice section: who provides it, which voice, its settings and how they talk."""
    has_voice = bool(voice.voice_id)
    settings = SETTINGS_TEXT.format(
        stability=voice.stability,
        similarity_boost=voice.similarity_boost,
        style=voice.style,
        speed=voice.speed,
        boost="on" if voice.use_speaker_boost else "off",
    )
    named = f"{voice.voice_name} ({voice.voice_id})" if voice.voice_name else voice.voice_id
    return {
        "provider": ELEVENLABS if has_voice else open_text("", "voice provider"),
        "voice": named if has_voice else open_text("", "voice"),
        "settings": settings if has_voice else open_text("", "voice settings"),
        "pace": open_text(voice.pace, "pace"),
        "tics": open_text(voice.tics, "verbal tics"),
        "uses": open_text(voice.uses, "words they use"),
        "never_uses": open_text(voice.never_uses, "words they never use"),
    }


def section(values: BaseModel, prefix: str) -> dict[str, str]:
    """A section of plain texts, each open when empty."""
    return {
        name: open_text(str(value), f"{prefix} {name.replace('_', ' ')}")
        for name, value in values.model_dump().items()
    }


def character_data(
    entry: LibraryEntry, slug: str, sheet: CharacterSheet, reels: dict[int, str]
) -> dict[str, Any]:
    """One cast.json character; the exporter has already checked its world and kind are set."""
    return {
        "slug": slug,
        "name": entry.name,
        "world": sheet.world.value if sheet.world else "",
        "status": sheet.status.value,
        "kind": sheet.character_kind.value if sheet.character_kind else "",
        "reels": reels_of(sheet.canon, reels),
        "look": {
            "drawing": [{"part": sheet.rig, "version": sheet.rig_version}] if sheet.rig else [],
            **section(sheet.look, "look"),
        },
        "voice": voice_data(sheet.voice),
        "who": section(sheet.who, "who"),
        "comic": {
            "role": sheet.comic.role.value if sheet.comic.role else open_text("", "comic role"),
            "under_pressure": open_text(sheet.comic.under_pressure, "under pressure"),
            "always_gets_them": open_text(sheet.comic.always_gets_them, "what always gets them"),
        },
        "canon": [fact_data(fact, reels) for fact in sheet.canon],
        "never": list(sheet.never),
        "open": [
            {"question": item.question, "proposal": open_text(item.proposal, "proposal")}
            for item in sheet.open
        ],
    }


def place_data(
    entry: LibraryEntry, slug: str, sheet: PlaceSheet, reels: dict[int, str]
) -> dict[str, Any]:
    """One cast.json place; the exporter has already checked its world is set."""
    return {
        "slug": slug,
        "name": entry.name,
        "world": sheet.world.value if sheet.world else "",
        "description": open_text(entry.description, "description"),
        "found": reels_of(sheet.cites, reels),
        "cites": [fact_data(fact, reels) for fact in sheet.cites],
    }


def relationship_data(
    pair: Relationship, slugs: dict[UUID, str], reels: dict[int, str]
) -> dict[str, Any]:
    """One relationships.json pair, written alphabetically as srstudio requires."""
    first, second = sorted((slugs[pair.first], slugs[pair.second]))
    texts = {
        name: open_text(getattr(pair, name), name.replace("_", " "))
        for name in ("relation", "how_they_know", "dynamic", "tension", "meet")
    }
    return {
        "a": first,
        "b": second,
        **texts,
        "history": [fact_data(fact, reels) for fact in pair.history],
        "status": pair.status.value,
        "reels": reels_of(pair.history, reels),
    }


def header(source: BibleSource) -> dict[str, str]:
    """The fields both bible files start with."""
    return {"schema": BIBLE_SCHEMA, "version": source.version, "generated_by": source.generated_by}


def cast_document(source: BibleSource) -> dict[str, Any]:
    """cast.json: the characters and places; sreditor keeps no roles, rules or clashes."""
    return {
        **header(source),
        "characters": [
            character_data(entry, slug, sheet, source.reels)
            for entry, slug, sheet in source.characters
        ],
        "roles": [],
        "places": [
            place_data(entry, slug, sheet, source.reels) for entry, slug, sheet in source.places
        ],
        "rules": [],
        "clashes": [],
    }


def relationships_document(source: BibleSource) -> dict[str, Any]:
    """relationships.json: every pair whose two characters are in the cast."""
    slugs = {entry.id: slug for entry, slug, _ in source.characters}
    pairs = [pair for pair in source.relationships if pair.first in slugs and pair.second in slugs]
    return {
        **header(source),
        "relationships": [relationship_data(pair, slugs, source.reels) for pair in pairs],
    }
