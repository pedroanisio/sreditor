"""Pauses a v3 take was asked for, put into its audio at their exact length."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from sreditor.audio_pauses import Silence, shifted, silences, spaced
from sreditor.block_takes import WordTiming, audio_seconds
from sreditor.voice_markup import SpeechPause

WORDS = [
    WordTiming(text="Wait", start=0.0, end=0.4),
    WordTiming(text="for", start=0.6, end=0.8),
    WordTiming(text="it", start=0.9, end=1.0),
]
SECOND_OF_TONE = 1.0


def test_silences_adds_only_what_the_voice_left_out() -> None:
    added = silences([SpeechPause(after_words=1, seconds=1.0)], WORDS, 1.2)
    assert added == [Silence(at=0.5, seconds=0.8)]


def test_silences_pause_shorter_than_the_gap_adds_nothing() -> None:
    assert silences([SpeechPause(after_words=1, seconds=0.1)], WORDS, 1.2) == []


def test_silences_pause_after_last_word_goes_before_the_end() -> None:
    added = silences([SpeechPause(after_words=3, seconds=0.5)], WORDS, 1.2)
    assert added == [Silence(at=1.1, seconds=0.3)]


def test_silences_two_pauses_at_one_point_join() -> None:
    pauses = [SpeechPause(after_words=1, seconds=0.5), SpeechPause(after_words=1, seconds=0.5)]
    assert silences(pauses, WORDS, 1.2) == [Silence(at=0.5, seconds=0.6)]


def test_shifted_moves_only_words_after_the_silence() -> None:
    moved = shifted(WORDS, [Silence(at=0.5, seconds=0.8)])
    assert [(word.start, word.end) for word in moved] == [(0.0, 0.4), (1.4, 1.6), (1.7, 1.8)]


def test_spaced_without_words_keeps_the_take() -> None:
    assert spaced(b"audio", [], [SpeechPause(after_words=0, seconds=1)], 1.0) is None


def test_spaced_unreadable_audio_keeps_the_take() -> None:
    assert spaced(b"not audio", WORDS, [SpeechPause(after_words=1, seconds=1)], 1.2) is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_spaced_real_mp3_grows_by_the_silence() -> None:
    tone = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=duration={SECOND_OF_TONE}",
         "-ac", "1", "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-f", "mp3", "pipe:1"],
        capture_output=True,
        check=True,
    ).stdout  # fmt: skip
    found = spaced(tone, WORDS, [SpeechPause(after_words=1, seconds=1.0)], SECOND_OF_TONE)
    assert found is not None and found.added_s == pytest.approx(0.8)
    assert audio_seconds(len(found.audio)) == pytest.approx(SECOND_OF_TONE + 0.8, abs=0.1)
