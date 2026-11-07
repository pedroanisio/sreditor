from __future__ import annotations

from datetime import UTC, datetime

from sreditor.assets import StoredFile
from sreditor.block_takes import (
    BlockTake,
    BlockVoice,
    WordTiming,
    audio_seconds,
    block_take_key,
    words_of,
)
from sreditor.elevenlabs import Alignment
from sreditor.short_script import ShortScriptInput
from sreditor.voice_generation import (
    BlockTiming,
    PlannedTime,
    joined_timings,
    language_code,
    spoken_blocks,
    suggested_times,
    take_key,
)
from sreditor.voice_markup import TtsModel
from sreditor.voice_tuning import VoiceTuning

NARRATOR = BlockVoice(voice_id="abc", voice_name="", model=TtsModel.V3, language="pt-BR")
TUNING = VoiceTuning(
    stability=0.5, similarity_boost=0.75, style=0.0, speed=1.0, use_speaker_boost=True
)


def aligned(text: str, step: float = 0.1) -> Alignment:
    return Alignment(
        characters=list(text),
        character_start_times_seconds=[round(index * step, 2) for index in range(len(text))],
        character_end_times_seconds=[round((index + 1) * step, 2) for index in range(len(text))],
    )


def script(**voice: str) -> ShortScriptInput:
    return ShortScriptInput.model_validate(
        {
            "duration_s": 5,
            "voice": {"voice_id": "abc", **voice},
            "blocks": [
                {"start_s": 0, "end_s": 2, "function": "hook", "speech": "Ab [pause:0.2] cd."},
                {"start_s": 2, "end_s": 5, "function": "punch", "speech": "[irony]Ef[/irony]."},
            ],
        }
    )


def take(duration_s: float, words: list[WordTiming]) -> BlockTake:
    """A block take of this length with these words."""
    stored = StoredFile(path="c/x.mp3", media_type="audio/mpeg", sha256="x", size_bytes=1)
    return BlockTake(
        key="k",
        text="t",
        voice=NARRATOR,
        file=stored,
        duration_s=duration_s,
        words=words,
        created_at=datetime(2026, 11, 7, tzinfo=UTC),
    )


def test_words_of_times_each_word_from_first_to_last_letter() -> None:
    words = words_of(aligned("Oi, tu."))
    assert [(word.text, word.start, word.end) for word in words] == [
        ("Oi", 0.0, 0.2),
        ("tu", 0.4, 0.6),
    ]


def test_words_of_skips_echoed_audio_tags() -> None:
    assert [word.text for word in words_of(aligned("[sighs] Ab"))] == ["Ab"]


def test_words_of_skips_echoed_v2_breaks() -> None:
    words = words_of(aligned('Ab <break time="0.2s" /> cd'))
    assert [word.text for word in words] == ["Ab", "cd"]


def test_words_of_skips_runs_of_punctuation_only() -> None:
    assert [word.text for word in words_of(aligned("Ab — cd"))] == ["Ab", "cd"]


def test_words_of_without_alignment_is_empty() -> None:
    assert words_of(None) == []


def test_block_take_key_changes_with_voice_tuning_model_and_text() -> None:
    keys = {
        block_take_key("Ab", NARRATOR),
        block_take_key("Ab", NARRATOR.model_copy(update={"tuning": TUNING})),
        block_take_key("Ab", NARRATOR.model_copy(update={"voice_id": "xyz"})),
        block_take_key("Ab", NARRATOR.model_copy(update={"model": TtsModel.V2})),
        block_take_key("Cd", NARRATOR),
    }
    assert len(keys) == 5


def test_block_take_key_is_stable_for_same_block() -> None:
    assert block_take_key("Ab", NARRATOR) == block_take_key("Ab", NARRATOR)


def test_take_key_changes_when_any_block_changes() -> None:
    assert take_key(["a", None]) != take_key(["a", "b"])


def test_audio_seconds_of_constant_bitrate_mp3() -> None:
    assert audio_seconds(16_000) == 1.0


def test_joined_timings_shift_each_block_by_the_takes_before() -> None:
    first = take(1.0, [WordTiming(text="Ab", start=0.1, end=0.8)])
    second = take(2.0, [WordTiming(text="Cd", start=0.2, end=1.5)])
    assert joined_timings([first, None, second]) == [
        BlockTiming(start=0.1, end=0.8),
        None,
        BlockTiming(start=1.2, end=2.5),
    ]


def test_joined_timings_take_without_words_spans_its_audio() -> None:
    assert joined_timings([take(1.5, [])]) == [BlockTiming(start=0.0, end=1.5)]


def test_suggested_times_end_each_block_where_the_next_starts() -> None:
    timings = [BlockTiming(start=0.1, end=0.9), None, BlockTiming(start=1.24, end=2.0)]
    assert suggested_times(timings, 2.3) == [
        PlannedTime(start_s=0.0, end_s=1.2),
        PlannedTime(start_s=1.2, end_s=1.2),
        PlannedTime(start_s=1.2, end_s=2.3),
    ]


def test_suggested_times_without_timings_is_none() -> None:
    assert suggested_times(None, 3.0) is None


def test_spoken_blocks_render_for_chosen_model() -> None:
    assert spoken_blocks(script()) == ["Ab [pause] cd.", "[sarcastically] Ef."]
    assert spoken_blocks(script(model="v2")) == ['Ab <break time="0.2s" /> cd.', "Ef."]


def test_language_code_keeps_primary_subtag() -> None:
    assert language_code("pt-BR") == "pt"
