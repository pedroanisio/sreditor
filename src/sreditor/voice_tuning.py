"""How a voice delivers its lines, kept apart so brand and channel models can use it without cycles."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class VoiceTuning(BaseModel):
    """How a voice delivers its lines: ElevenLabs' voice_settings (rule project.srstudio.voice)."""

    model_config = ConfigDict(frozen=True)

    stability: float
    similarity_boost: float
    style: float
    speed: float
    use_speaker_boost: bool
