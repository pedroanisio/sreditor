"""A voice-over with each block's take and when its words are said (rule project.voice.estimate).

An agent cannot listen to the audio, so each spoken block comes back with its words, timed twice:
from the start of its own take, and from the start of the joined voice-over people hear.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sreditor.block_takes import BlockTake, BlockVoice, VoiceLicense, WordTiming
from sreditor.voice_generation import Take, speech_span

# Times are reported to the millisecond, like the take's own timings.
TIME_DECIMALS = 3


class VoiceOverBlock(BaseModel):
    """One spoken block's take: its voice and license, and its words timed both ways."""

    model_config = ConfigDict(frozen=True)

    block_number: int
    key: str
    text: str
    voice: BlockVoice
    license: VoiceLicense | None
    duration_s: float
    # Seconds from the start of this block's own take.
    start_s: float
    end_s: float
    words: list[WordTiming]
    # Where this take begins in the joined voice-over, and its words and speech from there.
    offset_s: float
    joined_start_s: float
    joined_end_s: float
    joined_words: list[WordTiming]


class VoiceOverTake(Take):
    """A generated voice-over with the take of each spoken block."""

    block_takes: list[VoiceOverBlock] = Field(default_factory=list)


def shifted(word: WordTiming, offset: float) -> WordTiming:
    """The word as said in the joined voice-over, `offset` seconds after its take starts."""
    return WordTiming(
        text=word.text,
        start=round(offset + word.start, TIME_DECIMALS),
        end=round(offset + word.end, TIME_DECIMALS),
    )


def voice_over_block(number: int, take: BlockTake, offset: float) -> VoiceOverBlock:
    """A block take with its words timed from its own start and from the joined voice-over's."""
    start, end = speech_span(take)
    return VoiceOverBlock(
        block_number=number,
        key=take.key,
        text=take.text,
        voice=take.voice,
        license=take.license,
        duration_s=take.duration_s,
        start_s=start,
        end_s=end,
        words=take.words,
        offset_s=round(offset, TIME_DECIMALS),
        joined_start_s=round(offset + start, TIME_DECIMALS),
        joined_end_s=round(offset + end, TIME_DECIMALS),
        joined_words=[shifted(word, offset) for word in take.words],
    )


def voice_over_blocks(takes: list[BlockTake | None]) -> list[VoiceOverBlock]:
    """The spoken blocks in order; a block without speech has no take and is left out."""
    blocks: list[VoiceOverBlock] = []
    offset = 0.0
    for number, take in enumerate(takes, start=1):
        if take is None:
            continue
        blocks.append(voice_over_block(number, take, offset))
        offset += take.duration_s
    return blocks


def voice_over_of(take: Take, block_takes: list[BlockTake | None]) -> VoiceOverTake:
    """The voice-over with the take of each of its blocks."""
    return VoiceOverTake(**take.model_dump(), block_takes=voice_over_blocks(block_takes))
