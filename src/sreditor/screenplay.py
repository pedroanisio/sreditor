"""An episode's screenplay, written in Fountain and stored on its own (project.episodes)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from sreditor.database import Database
from sreditor.episode_texts import EpisodeTexts, PartChanged, TextKey, TextPart
from sreditor.fountain import outline

# About 150 pages of screenplay; far more than a social media episode needs.
SCREENPLAY_LENGTH = 200000

ScreenplayText = Annotated[str, StringConstraints(max_length=SCREENPLAY_LENGTH)]


class ScreenplayInput(BaseModel):
    """The whole screenplay, in Fountain."""

    model_config = ConfigDict(frozen=True)

    text: ScreenplayText


class Screenplay(BaseModel):
    """An episode's screenplay with the scenes, places and characters read from it."""

    model_config = ConfigDict(frozen=True)

    channel: str
    number: int
    text: str
    scenes: list[str]
    locations: list[str]
    characters: list[str]
    revision: int
    updated_at: datetime | None = None


def screenplay_key(channel: str, number: int) -> TextKey:
    """Where an episode's screenplay is stored."""
    return TextKey(part=TextPart.SCREENPLAY, channel=channel, number=number)


class ScreenplayStore:
    """Screenplays in the application database; deleting the episode deletes its screenplay."""

    def __init__(self, database: Database) -> None:
        self.texts = EpisodeTexts(database)

    def screenplay(self, channel: str, number: int) -> Screenplay:
        """The episode's screenplay, empty at revision 0 when nobody has written it."""
        stored = self.texts.read(screenplay_key(channel, number))
        found = outline(stored.text)
        return Screenplay(
            channel=channel,
            number=number,
            text=stored.text,
            scenes=found.scenes,
            locations=found.locations,
            characters=found.characters,
            revision=stored.revision,
            updated_at=stored.updated_at,
        )

    def write(
        self, channel: str, number: int, data: ScreenplayInput, expected_revision: int | None
    ) -> Screenplay:
        """Replace the screenplay; with `expected_revision`, only if it is still at it."""
        key = screenplay_key(channel, number)
        if self.texts.write(key, data.text, expected_revision) is None:
            current = self.texts.read(key).revision
            raise PartChanged(f"Screenplay of {channel} #{number}", current)
        return self.screenplay(channel, number)
