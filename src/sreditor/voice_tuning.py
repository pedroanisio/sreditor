"""How a voice delivers its lines, kept apart so brand and channel models can use it without cycles."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from sreditor.srstudio_vocabulary import MAX_SPEED, MIN_SPEED

# ElevenLabs takes these three settings as ratios.
Ratio = Annotated[float, Field(ge=0, le=1)]


class VoiceTuning(BaseModel):
    """How a voice delivers its lines: ElevenLabs' voice_settings (rule project.srstudio.voice)."""

    model_config = ConfigDict(frozen=True)

    stability: Ratio
    similarity_boost: Ratio
    style: Ratio
    speed: Annotated[float, Field(ge=MIN_SPEED, le=MAX_SPEED)]
    use_speaker_boost: bool
