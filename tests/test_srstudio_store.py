"""srstudio sheets, episode slugs and relationships in the database (rules project.srstudio.*)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.library import EntryInput, LibraryEntry, LibraryKind, LibraryStore
from sreditor.srstudio import (
    CharacterSheet,
    EpisodeSlugInput,
    Fact,
    PlaceSheet,
    RelationshipInput,
    RelationshipNotFound,
    SameCharacter,
    SheetInput,
    SheetMismatch,
    SlugTaken,
    SrstudioStore,
    StyleSheet,
    VoiceProfile,
    proposed_slug,
)
from sreditor.srstudio_vocabulary import FactField, FactKind, Style, World

CHANNEL = "funny-memes"
SEED = (
    (
        "INSERT INTO channels (slug, document, created_at, updated_at)"
        " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
    ),
    (
        "INSERT INTO episodes (channel_slug, number, title, status, created_at, updated_at)"
        " VALUES ('funny-memes', 1, 'Pilot', 'idea', '2026-01-01', '2026-01-01')"
    ),
)
DOG = CharacterSheet(world=World.OKAYVILLE, rig="toon-dog", voice=VoiceProfile(voice_id="abc123"))


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        for statement in SEED:
            connection.execute(statement)
    return database


@pytest.fixture
def store(database: Database) -> SrstudioStore:
    return SrstudioStore(database)


@pytest.fixture
def library(database: Database) -> LibraryStore:
    return LibraryStore(database)


def character(library: LibraryStore, name: str = "Rex") -> LibraryEntry:
    return library.create(CHANNEL, LibraryKind.CHARACTER, EntryInput(name=name))


def test_proposed_slug_drops_accents_and_joins_words() -> None:
    assert proposed_slug("Café da Manhã!") == "cafe-da-manha"


def test_proposed_slug_starting_with_digit_gets_prefix() -> None:
    assert proposed_slug("2019 milk") == "untitled-2019-milk"


def test_sheet_never_saved_is_proposal_at_revision_zero(
    store: SrstudioStore, library: LibraryStore
) -> None:
    sheet = store.sheet(character(library, "Big Rex"))
    assert sheet.slug == "big-rex" and sheet.revision == 0 and sheet.sheet == CharacterSheet()


def test_sheet_save_keeps_bible_fields(store: SrstudioStore, library: LibraryStore) -> None:
    rex = character(library)
    store.save_sheet(rex, SheetInput(slug="rex", sheet=DOG), 0)
    assert store.sheet(rex).sheet == DOG and store.sheet(rex).revision == 1


def test_sheet_stale_revision_is_refused(store: SrstudioStore, library: LibraryStore) -> None:
    rex = character(library)
    store.save_sheet(rex, SheetInput(slug="rex", sheet=DOG), 0)
    with pytest.raises(PartChanged):
        store.save_sheet(rex, SheetInput(slug="rex", sheet=CharacterSheet()), 0)


def test_sheet_without_revision_overwrites_latest(
    store: SrstudioStore, library: LibraryStore
) -> None:
    rex = character(library)
    store.save_sheet(rex, SheetInput(slug="rex", sheet=DOG), None)
    assert store.save_sheet(rex, SheetInput(slug="rex-2", sheet=DOG), None).revision == 2


def test_sheet_slug_taken_by_another_character_is_refused(
    store: SrstudioStore, library: LibraryStore
) -> None:
    store.save_sheet(character(library), SheetInput(slug="rex", sheet=DOG), 0)
    with pytest.raises(SlugTaken):
        store.save_sheet(character(library, "Rex 2"), SheetInput(slug="rex", sheet=DOG), 0)


def test_sheet_of_other_kind_is_refused(store: SrstudioStore, library: LibraryStore) -> None:
    with pytest.raises(SheetMismatch):
        store.save_sheet(character(library), SheetInput(slug="rex", sheet=PlaceSheet()), 0)


def test_sheets_of_entries_mix_saved_and_proposed(
    store: SrstudioStore, library: LibraryStore
) -> None:
    rex, fern = character(library), character(library, "Fern")
    store.save_sheet(rex, SheetInput(slug="rex", sheet=DOG), 0)
    found = store.sheets([rex, fern])
    assert found[rex.id].revision == 1 and found[fern.id].slug == "fern"


def test_sheets_of_no_entries_are_empty(store: SrstudioStore) -> None:
    assert store.sheets([]) == {}


def test_deleting_entry_deletes_its_sheet(
    store: SrstudioStore, library: LibraryStore, database: Database
) -> None:
    rex = character(library)
    store.save_sheet(rex, SheetInput(slug="rex", sheet=DOG), 0)
    library.delete(rex, None)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM library_sheets").fetchone()[0] == 0


def test_style_sheet_unknown_srstudio_style_is_invalid() -> None:
    with pytest.raises(ValidationError):
        StyleSheet.model_validate({"style": "pixel"})


def test_style_sheet_keeps_srstudio_style(store: SrstudioStore, library: LibraryStore) -> None:
    flat = library.create(CHANNEL, LibraryKind.STYLE, EntryInput(name="Flat"))
    saved = store.save_sheet(flat, SheetInput(slug="flat", sheet=StyleSheet(style=Style.TOON)), 0)
    assert isinstance(saved.sheet, StyleSheet) and saved.sheet.style is Style.TOON


def test_voice_speed_outside_srstudio_range_is_invalid() -> None:
    with pytest.raises(ValidationError):
        VoiceProfile(speed=1.5)


def test_episode_slug_never_saved_is_proposed_from_title(store: SrstudioStore) -> None:
    proposal = store.episode_slug(CHANNEL, 1, "The Pilot")
    assert (proposal.slug, proposal.seed, proposal.revision) == ("the-pilot", 1, 0)


def test_episode_slug_save_then_read(store: SrstudioStore) -> None:
    current = store.episode_slug(CHANNEL, 1, "Pilot")
    store.save_episode_slug(current, EpisodeSlugInput(slug="pilot", seed=7), None)
    assert store.episode_slug(CHANNEL, 1, "Pilot").seed == 7


def test_episode_slug_stale_revision_is_refused(store: SrstudioStore) -> None:
    current = store.episode_slug(CHANNEL, 1, "Pilot")
    store.save_episode_slug(current, EpisodeSlugInput(slug="pilot", seed=7), None)
    with pytest.raises(PartChanged):
        store.save_episode_slug(current, EpisodeSlugInput(slug="pilot", seed=8), 0)


def test_episode_slug_taken_by_another_episode_is_refused(
    store: SrstudioStore, database: Database
) -> None:
    with database.connect() as connection:
        connection.execute(SEED[1].replace("1, 'Pilot'", "2, 'Two'"))
    store.save_episode_slug(
        store.episode_slug(CHANNEL, 1, "Pilot"), EpisodeSlugInput(slug="pilot", seed=1), None
    )
    with pytest.raises(SlugTaken):
        store.save_episode_slug(
            store.episode_slug(CHANNEL, 2, "Two"), EpisodeSlugInput(slug="pilot", seed=2), None
        )


def pair(library: LibraryStore) -> RelationshipInput:
    fact = Fact(
        fact="They share a fridge.", field=FactField.RELATIONSHIP, episode=1, kind=FactKind.LINE,
        quote="That milk is mine.",
    )  # fmt: skip
    return RelationshipInput(
        first=character(library).id, second=character(library, "Fern").id, history=[fact]
    )


def test_relationship_add_and_list(store: SrstudioStore, library: LibraryStore) -> None:
    added = store.add_relationship(CHANNEL, pair(library))
    assert store.relationships(CHANNEL) == [added] and added.history[0].episode == 1


def test_relationship_with_same_character_is_refused(
    store: SrstudioStore, library: LibraryStore
) -> None:
    rex = character(library).id
    with pytest.raises(SameCharacter):
        store.add_relationship(CHANNEL, RelationshipInput(first=rex, second=rex))


def test_relationship_replace_with_stale_revision_is_refused(
    store: SrstudioStore, library: LibraryStore
) -> None:
    added = store.add_relationship(CHANNEL, pair(library))
    changed = added.model_copy(update={"tension": "Milk"})
    store.replace_relationship(added, changed, 1)
    with pytest.raises(PartChanged):
        store.replace_relationship(added, changed, 1)


def test_relationship_replace_with_same_character_is_refused(
    store: SrstudioStore, library: LibraryStore
) -> None:
    added = store.add_relationship(CHANNEL, pair(library))
    with pytest.raises(SameCharacter):
        store.replace_relationship(added, added.model_copy(update={"second": added.first}), None)


def test_relationship_remove_then_missing(store: SrstudioStore, library: LibraryStore) -> None:
    added = store.add_relationship(CHANNEL, pair(library))
    store.remove_relationship(added, 1)
    with pytest.raises(RelationshipNotFound):
        store.relationship(CHANNEL, added.id)


def test_relationship_remove_with_stale_revision_is_refused(
    store: SrstudioStore, library: LibraryStore
) -> None:
    added = store.add_relationship(CHANNEL, pair(library))
    with pytest.raises(PartChanged):
        store.remove_relationship(added, 5)
