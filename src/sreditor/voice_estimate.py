"""What generating a voice-over would cost, before spending anything (rule project.voice.estimate).

The estimate walks the same block plans as generate_voice_over and asks the same question it does
before calling ElevenLabs: is this block's take already stored with its audio? A block that is
stored is reused and costs nothing; every other block spends one credit per character (the rate
of its model is in elevenlabs.CREDITS_PER_CHARACTER).
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict

from sreditor.block_takes import BlockTakeRecord
from sreditor.deps import ChannelAccess, Services
from sreditor.elevenlabs import CREDITS_PER_CHARACTER, MODEL_IDS
from sreditor.episode_operations import episode_access
from sreditor.library import LibraryKind
from sreditor.members import Permission
from sreditor.short_script import Block, ShortScriptInput
from sreditor.short_script_operations import (
    NO_SPEECH,
    BlockPlan,
    ScriptNotReady,
    block_plans,
    has_audio,
)
from sreditor.voice_markup import TtsModel, plain_text

NARRATOR = "narrator"


class BlockEstimate(BaseModel):
    """One spoken block: who says it, with which voice, how long it is and whether it is paid for."""

    model_config = ConfigDict(frozen=True)

    # The position in the short script's blocks, counting from 1.
    block_number: int
    # "narrator" or the name of the library character who speaks.
    speaker: str
    voice_id: str
    voice_name: str
    # The characters of the speech without voice tags.
    characters: int
    # True when a stored take is reused and spends no credits.
    is_reused: bool


class VoiceEstimate(BaseModel):
    """The credits a voice-over would spend now, per block and in total."""

    model_config = ConfigDict(frozen=True)

    model: TtsModel
    model_id: str
    credits_per_character: float
    blocks: list[BlockEstimate]
    new_characters: int
    reused_characters: int
    estimated_credits: int


def speaker_name(app: Services, channel: str, block: Block) -> str:
    """The narrator, or the name of the library character the block names."""
    if block.speaker is None:
        return NARRATOR
    return app.library.get(channel, LibraryKind.CHARACTER, block.speaker).name


def is_stored(app: Services, channel: str, number: int, plan: BlockPlan) -> bool:
    """Whether generating would reuse a stored take for this plan, as spoken_take decides."""
    stored = app.block_takes.find(BlockTakeRecord(channel=channel, number=number, key=plan.key))
    return stored is not None and has_audio(app, stored)


def block_estimates(
    app: Services, channel: str, number: int, script: ShortScriptInput
) -> list[BlockEstimate]:
    """Each spoken block with what it would cost; a second block with the same take reuses the first."""
    plans = block_plans(app, channel, script)
    paid: set[str] = set()
    found: list[BlockEstimate] = []
    for position, (block, plan) in enumerate(zip(script.blocks, plans, strict=True), start=1):
        if plan is None:
            continue
        is_reused = plan.key in paid or is_stored(app, channel, number, plan)
        paid.add(plan.key)
        found.append(
            BlockEstimate(
                block_number=position,
                speaker=speaker_name(app, channel, block),
                voice_id=plan.voice.voice_id,
                voice_name=plan.voice.voice_name,
                characters=len(plain_text(block.speech)),
                is_reused=is_reused,
            )
        )
    return found


def estimate_voice_over(app: Services, access: ChannelAccess, number: int) -> VoiceEstimate:
    """The credits generating the saved short script's voice-over would spend; nothing is spent."""
    slug = episode_access(app, access, number, Permission.GENERATE_VOICE)
    script = app.short_scripts.short_script(slug, number)
    blocks = block_estimates(app, slug, number, script)
    if not blocks:
        raise ScriptNotReady(NO_SPEECH)
    rate = CREDITS_PER_CHARACTER[script.voice.model]
    new_characters = sum(block.characters for block in blocks if not block.is_reused)
    return VoiceEstimate(
        model=script.voice.model,
        model_id=MODEL_IDS[script.voice.model],
        credits_per_character=rate,
        blocks=blocks,
        new_characters=new_characters,
        reused_characters=sum(block.characters for block in blocks if block.is_reused),
        estimated_credits=math.ceil(new_characters * rate),
    )
