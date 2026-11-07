"""A small client for the ElevenLabs text-to-speech API (rule project.voice-generation).

It lists the account's voices and turns text into MP3 with per-character timestamps, so each
block's real start can be measured. The API key travels only in the xi-api-key header to
ElevenLabs and is never logged. The transport is injected, so tests never reach the paid API.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from sreditor.voice_markup import TtsModel

DEFAULT_ELEVENLABS_URL = "https://api.elevenlabs.io"


class VoiceSettings(BaseModel):
    """ElevenLabs access; without a key, voice-over generation is off (project.voice-generation)."""

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr | None = None
    url: str = DEFAULT_ELEVENLABS_URL


MODEL_IDS: dict[TtsModel, str] = {TtsModel.V3: "eleven_v3", TtsModel.V2: "eleven_multilingual_v2"}
OUTPUT_FORMAT = "mp3_44100_128"
MP3_BITS_PER_SECOND = 128_000
BITS_PER_BYTE = 8
# Rendering a minute of speech takes ElevenLabs well under this; Cloudflare gives up at 100 s.
TIMEOUT_SECONDS = 90.0
# The voice list rarely changes; asking once every few minutes keeps the page fast.
VOICES_TTL_SECONDS = 300.0
# Answers meaning "this model or voice cannot do that", so a simpler request may still work.
RETRY_SIMPLER = frozenset(
    {HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND, HTTPStatus.UNPROCESSABLE_ENTITY}
)
ERROR_LENGTH = 300

# (method, url, headers, body) -> (status, body)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class ElevenLabsError(RuntimeError):
    """ElevenLabs refused a request or could not be reached."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"ElevenLabs answered {status}: {reason}")
        self.status = status


def send(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    """Send one request; an HTTP error keeps its status and body, which name the reason."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            answer: bytes = response.read()
            return HTTPStatus.OK, answer
    except urllib.error.HTTPError as refusal:
        return refusal.code, refusal.read()
    except (urllib.error.URLError, TimeoutError) as failure:
        raise ElevenLabsError(
            HTTPStatus.BAD_GATEWAY, f"cannot reach ElevenLabs: {failure}"
        ) from failure


def reason_of(body: bytes) -> str:
    """The `detail` message of an ElevenLabs error, or the start of the raw answer."""
    try:
        detail = json.loads(body).get("detail", "")
    except (ValueError, AttributeError):
        return body.decode(errors="replace")[:ERROR_LENGTH]
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)[:ERROR_LENGTH]
    return str(detail)[:ERROR_LENGTH]


class Voice(BaseModel):
    """A voice the account can use, with what helps a person choose it."""

    model_config = ConfigDict(frozen=True)

    voice_id: str
    name: str
    category: str | None = None
    languages: list[str] = Field(default_factory=list)
    accent: str | None = None
    preview_url: str | None = None


def voice_from(raw: dict[str, Any]) -> Voice:
    """The fields people need, with languages merged from the verified list and the labels."""
    labels = raw.get("labels") or {}
    verified = raw.get("verified_languages") or []
    languages = {item["language"] for item in verified if item.get("language")}
    if labels.get("language"):
        languages.add(labels["language"])
    return Voice(
        voice_id=raw["voice_id"],
        name=raw["name"],
        category=raw.get("category"),
        languages=sorted(languages),
        accent=labels.get("accent"),
        preview_url=raw.get("preview_url"),
    )


class Alignment(BaseModel):
    """When each character of the sent text is spoken, in seconds."""

    model_config = ConfigDict(frozen=True)

    characters: list[str]
    character_start_times_seconds: list[float]
    character_end_times_seconds: list[float]


class Speech(BaseModel):
    """Audio from ElevenLabs, with its timestamps when the model gives them."""

    model_config = ConfigDict(frozen=True)

    audio: bytes
    alignment: Alignment | None = None
    language_code: str | None = None

    @property
    def duration_s(self) -> float:
        """The spoken length: the last character's end, else estimated from the bitrate."""
        if self.alignment and self.alignment.character_end_times_seconds:
            return self.alignment.character_end_times_seconds[-1]
        return len(self.audio) * BITS_PER_BYTE / MP3_BITS_PER_SECOND


class VoiceTuning(BaseModel):
    """How a voice delivers its lines: ElevenLabs' voice_settings (rule project.srstudio.voice)."""

    model_config = ConfigDict(frozen=True)

    stability: float
    similarity_boost: float
    style: float
    speed: float
    use_speaker_boost: bool


class SpeechRequest(BaseModel):
    """What to say, with which voice, tuning and model, in which language."""

    model_config = ConfigDict(frozen=True)

    text: str
    voice_id: str
    model: TtsModel
    language_code: str | None = None
    # None keeps the voice's own settings, as the narrator voice of a short script does.
    tuning: VoiceTuning | None = None


class ElevenLabs:
    """The account's voices and text-to-speech."""

    def __init__(self, api_key: SecretStr, base_url: str, transport: Transport = send) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.cached_voices: tuple[float, list[Voice]] | None = None

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, bytes]:
        """Send an authenticated request; the key goes only in its header."""
        headers = {"xi-api-key": self.api_key.get_secret_value(), "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        return self.transport(method, self.base_url + path, headers, body)

    def voices(self) -> list[Voice]:
        """The account's voices by name, asked again only after a few minutes."""
        now = time.monotonic()
        if self.cached_voices and now - self.cached_voices[0] < VOICES_TTL_SECONDS:
            return self.cached_voices[1]
        status, body = self.request("GET", "/v1/voices")
        if status != HTTPStatus.OK:
            raise ElevenLabsError(status, reason_of(body))
        found = sorted(
            (voice_from(raw) for raw in json.loads(body)["voices"]),
            key=lambda voice: voice.name.lower(),
        )
        self.cached_voices = (now, found)
        return found

    def speak(self, wanted: SpeechRequest) -> Speech:
        """MP3 for the text, with timestamps when the model gives them.

        Not every model takes a language code, and not every one gives timestamps, so a refusal
        is retried without the language, then without timestamps.
        """
        payload: dict[str, Any] = {"text": wanted.text, "model_id": MODEL_IDS[wanted.model]}
        if wanted.language_code:
            payload["language_code"] = wanted.language_code
        if wanted.tuning is not None:
            payload["voice_settings"] = wanted.tuning.model_dump()
        query = f"?output_format={OUTPUT_FORMAT}"
        timed = f"/v1/text-to-speech/{wanted.voice_id}/with-timestamps{query}"
        status, body = self.request("POST", timed, payload)
        if status in RETRY_SIMPLER and "language_code" in payload:
            payload.pop("language_code")
            status, body = self.request("POST", timed, payload)
        applied = payload.get("language_code")
        if status == HTTPStatus.OK:
            answer = json.loads(body)
            alignment = answer.get("alignment")
            return Speech(
                audio=base64.b64decode(answer["audio_base64"]),
                alignment=Alignment.model_validate(alignment) if alignment else None,
                language_code=applied,
            )
        if status not in RETRY_SIMPLER:
            raise ElevenLabsError(status, reason_of(body))
        status, body = self.request("POST", f"/v1/text-to-speech/{wanted.voice_id}{query}", payload)
        if status != HTTPStatus.OK:
            raise ElevenLabsError(status, reason_of(body))
        return Speech(audio=body, language_code=applied)


def build_voice(settings: VoiceSettings) -> ElevenLabs | None:
    """The ElevenLabs client, or None when no key is set."""
    if settings.api_key is None:
        return None
    return ElevenLabs(settings.api_key, settings.url)
