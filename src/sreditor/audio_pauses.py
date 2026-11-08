"""Put the pauses a v3 take was asked for into its audio, at their exact length (rule
project.voice-markup).

ElevenLabs v3 has no pause of a given length: its [pause] lasts what the model decides, about a
second whatever the script says. So v3 gets no pause tag, and each pause becomes silence placed
in the gap between the words around it, found from the take's word timestamps: only what the
voice did not already leave there is added, so a 0.3 s pause after a natural 0.2 s gap adds
0.1 s. The words after it move by the same amount. Without ffmpeg, or on audio it cannot read,
`spaced` returns None and the caller keeps the take as ElevenLabs made it.
"""

from __future__ import annotations

import subprocess

from pydantic import BaseModel, ConfigDict

from sreditor.audio_finish import BITRATE, FFMPEG, SAMPLE_RATE, TIMEOUT_SECONDS
from sreditor.block_takes import WordTiming
from sreditor.voice_markup import SpeechPause

# Times are kept to the millisecond, as the word timestamps are.
TIME_DECIMALS = 3


class Silence(BaseModel):
    """Silence to put into the audio: where, in seconds from the start, and how long."""

    model_config = ConfigDict(frozen=True)

    at: float
    seconds: float


class SpacedSpeech(BaseModel):
    """A take with its pauses in it: the new audio, the words at their new times, and the
    seconds added."""

    model_config = ConfigDict(frozen=True)

    audio: bytes
    words: list[WordTiming]
    added_s: float


def silence_for(pause: SpeechPause, words: list[WordTiming], duration: float) -> Silence | None:
    """The silence one pause still needs, in the middle of the gap it falls in; None when the
    voice already paused that long."""
    count = min(pause.after_words, len(words))
    before = words[count - 1].end if count else 0.0
    after = words[count].start if count < len(words) else duration
    gap = max(after - before, 0.0)
    missing = round(pause.seconds - gap, TIME_DECIMALS)
    if missing <= 0:
        return None
    return Silence(at=round(before + gap / 2, TIME_DECIMALS), seconds=missing)


def silences(pauses: list[SpeechPause], words: list[WordTiming], duration: float) -> list[Silence]:
    """The silences to add, in order, with pauses at one point joined into one."""
    joined: dict[float, float] = {}
    for pause in pauses:
        found = silence_for(pause, words, duration)
        if found is not None:
            joined[found.at] = joined.get(found.at, 0.0) + found.seconds
    return [Silence(at=at, seconds=seconds) for at, seconds in sorted(joined.items())]


def shifted(words: list[WordTiming], added: list[Silence]) -> list[WordTiming]:
    """Each word moved later by the silence put before it."""
    moved: list[WordTiming] = []
    for word in words:
        offset = sum(silence.seconds for silence in added if silence.at <= word.start)
        moved.append(
            word.model_copy(
                update={
                    "start": round(word.start + offset, TIME_DECIMALS),
                    "end": round(word.end + offset, TIME_DECIMALS),
                }
            )
        )
    return moved


def splice_filter(added: list[Silence]) -> str:
    """An ffmpeg filter that cuts the audio at each silence's point and pads each piece."""
    count = len(added) + 1
    inputs = "".join(f"[in{index}]" for index in range(count))
    pieces = [f"[0:a]asplit={count}{inputs}"]
    start = 0.0
    for index, silence in enumerate(added):
        pieces.append(
            f"[in{index}]atrim=start={start:.3f}:end={silence.at:.3f},asetpts=PTS-STARTPTS,"
            f"apad=pad_dur={silence.seconds:.3f}[piece{index}]"
        )
        start = silence.at
    last = len(added)
    pieces.append(f"[in{last}]atrim=start={start:.3f},asetpts=PTS-STARTPTS[piece{last}]")
    joined = "".join(f"[piece{index}]" for index in range(count))
    pieces.append(f"{joined}concat=n={count}:v=0:a=1[out]")
    return ";".join(pieces)


def splice_command(added: list[Silence]) -> list[str]:
    """ffmpeg reading MP3 on stdin and writing it with the silences in to stdout."""
    return [
        FFMPEG, "-v", "error", "-f", "mp3", "-i", "pipe:0",
        "-filter_complex", splice_filter(added), "-map", "[out]",
        "-c:a", "libmp3lame", "-b:a", BITRATE, "-ar", SAMPLE_RATE, "-f", "mp3", "pipe:1",
    ]  # fmt: skip


def spliced(audio: bytes, added: list[Silence]) -> bytes | None:
    """The audio with the silences put in, or None when ffmpeg cannot do it."""
    try:
        result = subprocess.run(
            splice_command(added),
            input=audio,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def spaced(
    audio: bytes, words: list[WordTiming], pauses: list[SpeechPause], duration: float
) -> SpacedSpeech | None:
    """The take with each pause at its length; None when nothing is missing or ffmpeg fails."""
    added = silences(pauses, words, duration) if words else []
    if not added:
        return None
    new_audio = spliced(audio, added)
    if new_audio is None:
        return None
    return SpacedSpeech(
        audio=new_audio,
        words=shifted(words, added),
        added_s=round(sum(silence.seconds for silence in added), TIME_DECIMALS),
    )
