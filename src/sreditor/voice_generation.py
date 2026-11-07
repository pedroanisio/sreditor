"""Voice-over takes generated from a short script (rule project.voice-generation).

Each block is its own take in its speaker's voice (block_takes.py); the voice-over people hear is
those takes joined. It is keyed by a hash of its block takes' keys, so asking again for the same
script reuses it and spends no credits. Each block's real start comes from the lengths of the
block takes before it, so the planned times can be replaced by what the voices actually did.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from sreditor.block_takes import BlockTake
from sreditor.database import Database
from sreditor.short_script import ShortScriptInput
from sreditor.voice_markup import for_speech

KEY_LENGTH = 16
# Planned times are written to a tenth of a second.
TIME_DECIMALS = 1


class BlockTiming(BaseModel):
    """When a block's speech starts and ends in the audio."""

    model_config = ConfigDict(frozen=True)

    start: float
    end: float


class PlannedTime(BaseModel):
    """A block's suggested place in the video, with no gap before the next block."""

    model_config = ConfigDict(frozen=True)

    start_s: float
    end_s: float


class Take(BaseModel):
    """A generated voice-over: the media file it became and what was measured in it."""

    model_config = ConfigDict(frozen=True)

    key: str
    media_id: UUID
    text: str
    model: str
    voice_id: str
    language: str
    duration_s: float
    timings: list[BlockTiming | None] | None
    suggested: list[PlannedTime] | None
    created_at: datetime
    # The block take of each block, None for a block without speech; None for takes made
    # before each block had its own take.
    blocks: list[str | None] | None = None
    is_reused: bool = False


class TakeNotFound(LookupError):
    """The episode has no take with that key."""


def spoken_blocks(script: ShortScriptInput) -> list[str]:
    """Each block's speech as the chosen model receives it."""
    return [for_speech(block.speech, script.voice.model) for block in script.blocks]


def take_key(block_keys: list[str | None]) -> str:
    """The hash that makes two requests for the same blocks the same voice-over."""
    return hashlib.sha256(json.dumps(block_keys).encode()).hexdigest()[:KEY_LENGTH]


def language_code(language: str) -> str:
    """The ISO 639-1 code ElevenLabs takes: a BCP 47 tag's primary part."""
    return language.split("-")[0]


def speech_span(take: BlockTake) -> tuple[float, float]:
    """Where a take's speech starts and ends in the take itself; a take without words spans its audio."""
    if take.words:
        return take.words[0].start, take.words[-1].end
    return 0.0, take.duration_s


def joined_timings(takes: list[BlockTake | None]) -> list[BlockTiming | None]:
    """Each block's speech in the joined voice-over: its take's words, shifted by the takes before it.

    A take without word timings spans its whole audio; a block without speech has no timing.
    """
    found: list[BlockTiming | None] = []
    offset = 0.0
    for take in takes:
        if take is None:
            found.append(None)
            continue
        first, last = speech_span(take)
        found.append(BlockTiming(start=round(offset + first, 3), end=round(offset + last, 3)))
        offset += take.duration_s
    return found


def suggested_times(
    timings: list[BlockTiming | None] | None, duration: float
) -> list[PlannedTime] | None:
    """Back-to-back times: each block ends where the next one's speech starts."""
    if timings is None:
        return None
    planned: list[PlannedTime] = []
    cursor = 0.0
    for index in range(len(timings)):
        next_start = next((timing.start for timing in timings[index + 1 :] if timing), duration)
        end = round(next_start, TIME_DECIMALS)
        planned.append(PlannedTime(start_s=cursor, end_s=end))
        cursor = end
    return planned


TAKE_COLUMNS = (
    "take_key, media_id, text, model, voice_id, language, duration_s, timings, suggested,"
    " created_at, blocks"
)


class TakeRecord(BaseModel):
    """Where a take is stored: its episode and key."""

    model_config = ConfigDict(frozen=True)

    channel: str
    number: int
    key: str


class TakeStore:
    """Takes in the application database; deleting the episode deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def now(self) -> datetime:
        """The time a new take is made, as the database records it."""
        return datetime.fromisoformat(self.database.now())

    def latest(self, channel: str, number: int) -> Take | None:
        """The episode's most recent take, if any."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT take_key FROM voice_takes WHERE channel_slug = ? AND number = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (channel, number),
            ).fetchone()
        if row is None:
            return None
        return self.find(TakeRecord(channel=channel, number=number, key=row["take_key"]))

    def find(self, record: TakeRecord) -> Take | None:
        """The episode's take with this key, if one was made."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {TAKE_COLUMNS} FROM voice_takes"
                " WHERE channel_slug = ? AND number = ? AND take_key = ?",
                (record.channel, record.number, record.key),
            ).fetchone()
        if row is None:
            return None
        return Take(
            key=row["take_key"],
            media_id=UUID(row["media_id"]),
            text=row["text"],
            model=row["model"],
            voice_id=row["voice_id"],
            language=row["language"],
            duration_s=row["duration_s"],
            timings=json.loads(row["timings"]),
            suggested=json.loads(row["suggested"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            blocks=json.loads(row["blocks"]) if row["blocks"] else None,
        )

    def get(self, record: TakeRecord) -> Take:
        """The take with this key; TakeNotFound when there is none."""
        found = self.find(record)
        if found is None:
            raise TakeNotFound(record.key)
        return found

    def save(self, channel: str, number: int, take: Take) -> None:
        """Remember a take, replacing an earlier one with the same key whose file was removed."""
        timings = [None if item is None else item.model_dump() for item in take.timings or []]
        suggested = [item.model_dump() for item in take.suggested or []]
        with self.database.connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO voice_takes (channel_slug, number, {TAKE_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    channel,
                    number,
                    take.key,
                    str(take.media_id),
                    take.text,
                    take.model,
                    take.voice_id,
                    take.language,
                    take.duration_s,
                    json.dumps(timings if take.timings is not None else None),
                    json.dumps(suggested if take.suggested is not None else None),
                    take.created_at.isoformat(),
                    None if take.blocks is None else json.dumps(take.blocks),
                ),
            )
