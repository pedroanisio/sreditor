"""One take per short-script block, in its speaker's voice, with word timings (rule project.voice-generation).

Each block is sent to ElevenLabs on its own, so every character speaks with the voice and tuning of
their srstudio sheet (rule project.srstudio.voice) and srstudio gets one take per line with the time
of each word (rule project.srstudio). A block take is keyed by its text, voice, tuning, model and
language: the same block asked again reuses its audio and spends no credits, even when the blocks
around it changed. Its audio lives in the AssetStore, named by content hash, and only the exporter
reads it; people hear the takes joined into one voice_over media file.
"""

from __future__ import annotations

import hashlib
import json
import string
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from sreditor.assets import StoredFile
from sreditor.database import Database
from sreditor.elevenlabs import (
    MODEL_IDS,
    MP3_BITS_PER_SECOND,
    PROVIDER,
    UNKNOWN_PLAN,
    Alignment,
)
from sreditor.voice_markup import TtsModel
from sreditor.voice_tuning import VoiceTuning

KEY_LENGTH = 16
BITS_PER_BYTE = 8
# Markup the model may echo in its timestamps: v3 audio tags [laughs], v2 <break time="1s"/>.
MARKUP_OPENERS = frozenset("[<")
MARKUP_CLOSERS = frozenset("]>")
PUNCTUATION = string.punctuation + "«»“”‘’¿¡…—–"
TAKE_COLUMNS = "take_key, text, voice, file, duration_s, words, created_at, license"


class WordTiming(BaseModel):
    """When a word is said, in seconds from the start of its take."""

    model_config = ConfigDict(frozen=True)

    text: str
    start: float
    end: float


class BlockVoice(BaseModel):
    """Who says a block: an ElevenLabs voice with its tuning, model and language."""

    model_config = ConfigDict(frozen=True)

    voice_id: str
    voice_name: str
    model: TtsModel
    language: str
    tuning: VoiceTuning | None = None


class VoiceLicense(BaseModel):
    """What a take was made with, kept to show where its voice came from (rule project.voice.estimate)."""

    model_config = ConfigDict(frozen=True)

    provider: str
    voice_id: str
    voice_name: str
    model: str
    # The account's ElevenLabs plan when the take was made, or "unknown".
    plan: str
    created_at: datetime


class BlockTake(BaseModel):
    """A block's speech as one audio file, with when each word is said."""

    model_config = ConfigDict(frozen=True)

    key: str
    text: str
    voice: BlockVoice
    file: StoredFile
    duration_s: float
    words: list[WordTiming]
    created_at: datetime
    # None only for a take whose model the license backfill did not recognise.
    license: VoiceLicense | None = None


def block_take_key(text: str, voice: BlockVoice) -> str:
    """The hash that makes two requests for the same block the same take."""
    tuning = voice.tuning.model_dump() if voice.tuning else None
    payload = json.dumps(
        [text, MODEL_IDS[voice.model], voice.voice_id, voice.language, tuning],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:KEY_LENGTH]


def audio_seconds(size_bytes: int) -> float:
    """How long ElevenLabs' constant-bitrate MP3 of this size plays."""
    return round(size_bytes * BITS_PER_BYTE / MP3_BITS_PER_SECOND, 3)


def word_runs(characters: list[str]) -> list[list[int]]:
    """Positions of each run of characters between spaces, leaving out echoed markup."""
    runs: list[list[int]] = [[]]
    depth = 0
    for index, character in enumerate(characters):
        if character in MARKUP_OPENERS or character in MARKUP_CLOSERS or character.isspace():
            depth += (character in MARKUP_OPENERS) - (character in MARKUP_CLOSERS)
            depth = max(depth, 0)
            runs.append([])
        elif depth == 0:
            runs[-1].append(index)
    return [run for run in runs if any(characters[index].isalnum() for index in run)]


def word_of(run: list[int], alignment: Alignment) -> WordTiming:
    """A run as a word, timed from its first to its last letter or digit."""
    letters = [index for index in run if alignment.characters[index].isalnum()]
    text = "".join(alignment.characters[index] for index in run).strip(PUNCTUATION)
    return WordTiming(
        text=text,
        start=alignment.character_start_times_seconds[letters[0]],
        end=alignment.character_end_times_seconds[letters[-1]],
    )


def words_of(alignment: Alignment | None) -> list[WordTiming]:
    """Each spoken word with its time, from ElevenLabs' character timestamps; none without them."""
    if alignment is None:
        return []
    return [word_of(run, alignment) for run in word_runs(alignment.characters)]


class BlockTakeRecord(BaseModel):
    """Where a block take is stored: its episode and key."""

    model_config = ConfigDict(frozen=True)

    channel: str
    number: int
    key: str


class BlockTakeStore:
    """Block takes in the application database; deleting the episode deletes them."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.backfill_licenses()

    def backfill_licenses(self) -> None:
        """Give takes made before license records one, with the plan unknown (project.voice.estimate)."""
        with self.database.connect() as connection:
            for model, model_id in MODEL_IDS.items():
                connection.execute(
                    "UPDATE voice_block_takes SET license = json_object("
                    "'provider', ?, 'voice_id', json_extract(voice, '$.voice_id'),"
                    " 'voice_name', json_extract(voice, '$.voice_name'), 'model', ?, 'plan', ?,"
                    " 'created_at', created_at)"
                    " WHERE license IS NULL AND json_extract(voice, '$.model') = ?",
                    (PROVIDER, model_id, UNKNOWN_PLAN, model.value),
                )

    def find(self, record: BlockTakeRecord) -> BlockTake | None:
        """The episode's block take with this key, if one was made."""
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {TAKE_COLUMNS} FROM voice_block_takes"
                " WHERE channel_slug = ? AND number = ? AND take_key = ?",
                (record.channel, record.number, record.key),
            ).fetchone()
        if row is None:
            return None
        return BlockTake(
            key=row["take_key"],
            text=row["text"],
            voice=BlockVoice.model_validate_json(row["voice"]),
            file=StoredFile.model_validate_json(row["file"]),
            duration_s=row["duration_s"],
            words=[WordTiming.model_validate(word) for word in json.loads(row["words"])],
            created_at=datetime.fromisoformat(row["created_at"]),
            license=VoiceLicense.model_validate_json(row["license"]) if row["license"] else None,
        )

    def save(self, channel: str, number: int, take: BlockTake) -> None:
        """Remember a block take, replacing an earlier one with the same key."""
        with self.database.connect() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO voice_block_takes (channel_slug, number, {TAKE_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    channel,
                    number,
                    take.key,
                    take.text,
                    take.voice.model_dump_json(),
                    take.file.model_dump_json(),
                    take.duration_s,
                    json.dumps([word.model_dump() for word in take.words]),
                    take.created_at.isoformat(),
                    take.license.model_dump_json() if take.license else None,
                ),
            )
