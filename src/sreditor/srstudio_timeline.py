"""srstudio's take timeline, poses and voice direction, written from a short script and its block takes (rule project.srstudio).

Each spoken block is one take: its audio becomes audio/take-<n>.wav (48 kHz mono 16-bit, as srstudio
records), and the takes run back to back from 0 in timeline.json with their words timed from the
take's start. A block's voice markup becomes srstudio's direction (tags, quality, emphasis, pauses)
next to the clean line text. Beats become assets/poses.json, each placed in its take by scaling its
time within the planned block to the take's real length, so no beat leaves its block.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import wave
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from sreditor.block_takes import BlockTake
from sreditor.elevenlabs import MODEL_IDS
from sreditor.field_types import Sha256
from sreditor.short_script import Beat, Block
from sreditor.srstudio_vocabulary import AUDIO_TAGS
from sreditor.voice_markup import (
    TAG_PATTERN,
    VOCABULARY,
    TtsModel,
    ValueKind,
    is_pause_length,
    plain_text,
)

FFMPEG = "ffmpeg"
SAMPLE_RATE = 48_000
TIMEOUT_SECONDS = 120
CHUNK_BYTES = 1 << 20
TIME_DECIMALS = 6  # srstudio rounds recorded times to six places
MIN_PAUSE_SECONDS = 0.1  # srstudio/direction.py Pause
MAX_PAUSE_SECONDS = 3.0
EMPHASIS_TAG = "emphasis"
AUDIO_FOLDER = "audio"
WORD_PATTERN = re.compile(r"\w+")


class WavTake(BaseModel):
    """A block take converted for srstudio: where it goes in the zip, its file, length and hash."""

    model_config = ConfigDict(frozen=True)

    name: str
    path: Path
    duration_s: float
    sha256: Sha256


class TimedBlock(BaseModel):
    """A spoken block with who says it, its take and where that take starts in the timeline."""

    model_config = ConfigDict(frozen=True)

    index: int
    block: Block
    actor: str
    take: BlockTake
    wav: WavTake
    start: float


def wav_command(source: Path, target: Path) -> list[str]:
    """ffmpeg turning a take's MP3 into srstudio's WAV."""
    return [
        FFMPEG, "-v", "error", "-y", "-i", str(source),
        "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(target),
    ]  # fmt: skip


def to_wav(source: Path, target: Path) -> bool:
    """Convert a take to WAV; False when ffmpeg is missing or cannot read it."""
    try:
        result = subprocess.run(
            wav_command(source, target), capture_output=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and target.is_file()


def wav_seconds(path: Path) -> float:
    """A WAV file's exact length from its frame count."""
    with wave.open(str(path), "rb") as audio:
        return round(audio.getnframes() / audio.getframerate(), TIME_DECIMALS)


def file_sha256(path: Path) -> str:
    """A file's SHA-256, read a chunk at a time."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def wav_take(position: int, source: Path, folder: Path) -> WavTake | None:
    """The take at this position of the timeline as a WAV in `folder`, or None when it cannot be made."""
    name = f"{AUDIO_FOLDER}/take-{position:02d}.wav"
    target = folder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not to_wav(source, target):
        return None
    return WavTake(
        name=name, path=target, duration_s=wav_seconds(target), sha256=file_sha256(target)
    )


def pause_after(words: list[str], seconds: str) -> dict[str, Any] | None:
    """A pause after the last word said so far, counting earlier occurrences of that word."""
    if not words or not is_pause_length(seconds):
        return None
    length = min(max(float(seconds), MIN_PAUSE_SECONDS), MAX_PAUSE_SECONDS)
    last = words[-1]
    return {"after": last, "seconds": length, "nth": words[:-1].count(last)}


def direction_of(speech: str, model: TtsModel) -> dict[str, Any]:
    """srstudio's direction for a line: audio tags it knows, the rest as quality, stressed words and pauses."""
    tags: list[str] = []
    quality: list[str] = []
    emphasis: list[str] = []
    pauses: list[dict[str, Any]] = []
    words: list[str] = []
    stressed_from: int | None = None
    position = 0
    for match in TAG_PATTERN.finditer(speech):
        words += WORD_PATTERN.findall(speech[position : match.start()])
        position = match.end()
        closing, name, value = match.group(1) == "/", match.group(2), match.group(3)
        tag = VOCABULARY.get(name)
        if tag is None:
            continue
        if name == EMPHASIS_TAG:
            if closing and stressed_from is not None:
                emphasis += words[stressed_from:]
            stressed_from = None if closing else len(words)
        elif tag.value == ValueKind.SECONDS and value:
            pause = pause_after(words, value)
            pauses += [pause] if pause else []
        elif not closing and tag.v3:
            audio_tag = tag.v3.strip("[]")
            target = tags if audio_tag in AUDIO_TAGS else quality
            if audio_tag not in target:
                target.append(audio_tag)
    direction: dict[str, Any] = {"model": MODEL_IDS[model], "tags": tags, "emphasis": emphasis}
    direction["pauses"] = pauses
    if quality:
        direction["quality"] = ", ".join(quality)
    return direction


def timeline_item(timed: TimedBlock, model: TtsModel) -> dict[str, Any]:
    """One take of timeline.json: the clean line, its clock, its audio and its words."""
    duration = timed.wav.duration_s
    words = [
        {"text": word.text, "start": min(word.start, duration), "end": min(word.end, duration)}
        for word in timed.take.words
    ]
    return {
        "id": f"{timed.index + 1:02d}",
        "actor": timed.actor,
        "shot": timed.block.function.value,
        "text": plain_text(timed.block.speech),
        "start": round(timed.start, TIME_DECIMALS),
        "duration": duration,
        "speech_duration": words[-1]["end"] if words else duration,
        "audio": timed.wav.name,
        "sha256": timed.wav.sha256,
        "words": words,
        "direction": direction_of(timed.block.speech, model),
    }


def timeline_document(blocks: list[TimedBlock], model: TtsModel) -> dict[str, Any]:
    """timeline.json: contiguous takes from 0 and the total length."""
    end = blocks[-1].start + blocks[-1].wav.duration_s if blocks else 0.0
    return {
        "duration": round(end, TIME_DECIMALS),
        "items": [timeline_item(timed, model) for timed in blocks],
    }


def beat_start(timed: TimedBlock, beat: Beat) -> float:
    """Where a beat lands in the timeline: its share of the planned block, on the take's clock."""
    planned = timed.block.end_s - timed.block.start_s
    return round(timed.start + beat.at_s * timed.wav.duration_s / planned, TIME_DECIMALS)


def pose_of(beat: Beat) -> dict[str, Any]:
    """A beat's pose; wear only when something is worn, as srstudio writes it."""
    pose: dict[str, Any] = {
        "eyes": beat.eyes.value,
        "mouth": beat.mouth.value,
        "arms": beat.arms.value,
    }
    if beat.wear:
        pose["wear"] = [item.value for item in beat.wear]
    return pose


def placed_beats(blocks: list[TimedBlock], cast: dict[str, str]) -> list[tuple[float, str, Beat]]:
    """Every beat with its timeline start and the toon it shows, in order."""
    return [
        (beat_start(timed, beat), cast[str(beat.character)], beat)
        for timed in blocks
        for beat in timed.block.beats
    ]


def poses_document(
    blocks: list[TimedBlock], cast: dict[str, str], end: float
) -> list[dict[str, Any]]:
    """assets/poses.json: one appearance per run of beats of one toon, its poses counted from its start."""
    appearances: list[dict[str, Any]] = []
    placed = placed_beats(blocks, cast)
    for position, (start, who, beat) in enumerate(placed):
        stop = placed[position + 1][0] if position + 1 < len(placed) else round(end, TIME_DECIMALS)
        pose = pose_of(beat)
        last = appearances[-1] if appearances else None
        if last is not None and last["who"] == who and last["end"] == start:
            if {key: value for key, value in last["poses"][-1].items() if key != "start"} != pose:
                last["poses"].append({"start": round(start - last["start"], TIME_DECIMALS), **pose})
            last["end"] = stop
        else:
            appearances.append(
                {"who": who, "start": start, "end": stop, "poses": [{"start": 0.0, **pose}]}
            )
    return appearances


def events_document(blocks: list[TimedBlock]) -> list[dict[str, Any]]:
    """assets/events.json: each prop event as srstudio names it, "prop" or "prop:action"."""
    return [
        {
            "start": beat_start(timed, beat),
            "event": f"{beat.prop}:{beat.action}" if beat.action else beat.prop,
        }
        for timed in blocks
        for beat in timed.block.beats
        if beat.prop
    ]
