"""End generated speech gently instead of on a hard stop (rule project.voice-generation).

ElevenLabs cuts its audio right after the last sound, still at speech level, so players and
editors hear a click. `finish` fades the last moment out and adds a little silence. It works on
the reversed stream, so "the end" becomes the start: drop the encoder's digital silence there,
fade in, reverse back. It only drops sound below -60 dB, never speech, because the voice decays
past the last character's timestamp. Without ffmpeg, or on audio it cannot read, it returns
None and the caller keeps the original.
"""

from __future__ import annotations

import subprocess

FFMPEG = "ffmpeg"
TAIL_SECONDS = 0.6
FADE_SECONDS = 0.05
SILENCE_DB = -60
BITRATE = "128k"  # the same as ElevenLabs' mp3_44100_128
SAMPLE_RATE = "44100"
TIMEOUT_SECONDS = 60


def finish_command() -> list[str]:
    """ffmpeg reading MP3 on stdin and writing the finished MP3 to stdout."""
    filters = (
        f"areverse,silenceremove=start_periods=1:start_threshold={SILENCE_DB}dB,"
        f"afade=t=in:d={FADE_SECONDS:.3f},areverse,apad=pad_dur={TAIL_SECONDS:.3f}"
    )
    return [
        FFMPEG, "-v", "error", "-f", "mp3", "-i", "pipe:0", "-af", filters,
        "-c:a", "libmp3lame", "-b:a", BITRATE, "-ar", SAMPLE_RATE, "-f", "mp3", "pipe:1",
    ]  # fmt: skip


def finish(audio: bytes) -> bytes | None:
    """The audio with a short fade and silence at its end, or None when ffmpeg cannot do it."""
    try:
        result = subprocess.run(
            finish_command(), input=audio, capture_output=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None
