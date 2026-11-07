from __future__ import annotations

import base64
import json
import shutil
import subprocess
from http import HTTPStatus

import pytest
from pydantic import SecretStr

from sreditor import audio_finish
from sreditor.elevenlabs import (
    MP3_BITS_PER_SECOND,
    Alignment,
    ElevenLabs,
    ElevenLabsError,
    Speech,
    SpeechRequest,
    VoiceTuning,
)
from sreditor.voice_markup import TtsModel

KEY = SecretStr("xi-test-key")
BASE = "https://voice.test"
VOICES = {
    "voices": [
        {
            "voice_id": "v2",
            "name": "zoe",
            "labels": {"accent": "british", "language": "en"},
            "verified_languages": [{"language": "pt"}],
        },
        {"voice_id": "v1", "name": "Ana", "category": "premade"},
    ]
}
ALIGNED = {
    "audio_base64": base64.b64encode(b"mp3").decode(),
    "alignment": {
        "characters": ["O", "i"],
        "character_start_times_seconds": [0.0, 0.1],
        "character_end_times_seconds": [0.1, 0.4],
    },
}

Answer = tuple[int, bytes]


class FakeTransport:
    """Answers queued requests in order and records what was sent."""

    def __init__(self, *answers: Answer) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> Answer:
        self.sent.append((method, url, headers, json.loads(body) if body else None))
        return self.answers.pop(0)


def ok(payload: object) -> Answer:
    return HTTPStatus.OK, json.dumps(payload).encode()


def refused(status: int, message: str = "nope") -> Answer:
    return status, json.dumps({"detail": {"message": message}}).encode()


def client(*answers: Answer) -> tuple[ElevenLabs, FakeTransport]:
    transport = FakeTransport(*answers)
    return ElevenLabs(KEY, BASE + "/", transport), transport


REQUEST = SpeechRequest(text="Oi", voice_id="v1", model=TtsModel.V3, language_code="pt")


def test_voices_sorted_by_name_with_merged_languages() -> None:
    voices, _ = client(ok(VOICES))
    found = voices.voices()
    assert [voice.name for voice in found] == ["Ana", "zoe"]
    assert found[1].languages == ["en", "pt"] and found[1].accent == "british"


def test_voices_asked_again_within_minutes_uses_cache() -> None:
    voices, transport = client(ok(VOICES))
    voices.voices()
    voices.voices()
    assert len(transport.sent) == 1


def test_voices_refused_raises_with_reason() -> None:
    voices, _ = client(refused(HTTPStatus.UNAUTHORIZED, "invalid key"))
    with pytest.raises(ElevenLabsError, match="401: invalid key") as raised:
        voices.voices()
    assert raised.value.status == HTTPStatus.UNAUTHORIZED


def test_request_sends_key_only_in_header() -> None:
    voices, transport = client(ok(VOICES))
    voices.voices()
    method, url, headers, _ = transport.sent[0]
    assert (method, url) == ("GET", BASE + "/v1/voices")
    assert headers["xi-api-key"] == "xi-test-key" and "xi-test-key" not in url


def test_speak_with_timestamps_returns_audio_and_alignment() -> None:
    voices, transport = client(ok(ALIGNED))
    speech = voices.speak(REQUEST)
    assert speech.audio == b"mp3" and speech.language_code == "pt"
    assert speech.alignment is not None and speech.duration_s == 0.4
    _, url, _, body = transport.sent[0]
    assert url.endswith("/v1/text-to-speech/v1/with-timestamps?output_format=mp3_44100_128")
    assert body == {"text": "Oi", "model_id": "eleven_v3", "language_code": "pt"}


def test_speak_with_tuning_sends_voice_settings() -> None:
    voices, transport = client(ok(ALIGNED))
    tuning = VoiceTuning(
        stability=0.4, similarity_boost=0.8, style=0.2, speed=1.1, use_speaker_boost=True
    )
    voices.speak(REQUEST.model_copy(update={"tuning": tuning}))
    body = transport.sent[0][3] or {}
    assert body["voice_settings"] == tuning.model_dump()


def test_speak_refusing_language_retries_without_it() -> None:
    voices, transport = client(refused(HTTPStatus.UNPROCESSABLE_ENTITY), ok(ALIGNED))
    speech = voices.speak(REQUEST)
    assert speech.language_code is None
    assert "language_code" not in (transport.sent[1][3] or {})


def test_speak_without_timestamps_falls_back_to_plain_audio() -> None:
    voices, transport = client(
        refused(HTTPStatus.BAD_REQUEST), refused(HTTPStatus.BAD_REQUEST), (HTTPStatus.OK, b"raw")
    )
    speech = voices.speak(REQUEST)
    assert speech.audio == b"raw" and speech.alignment is None
    assert "with-timestamps" not in transport.sent[2][1]


def test_speak_with_server_error_raises() -> None:
    voices, _ = client(refused(HTTPStatus.INTERNAL_SERVER_ERROR, "boom"))
    with pytest.raises(ElevenLabsError, match="500: boom"):
        voices.speak(REQUEST.model_copy(update={"language_code": None}))


def test_speak_with_plain_endpoint_refused_raises() -> None:
    voices, _ = client(refused(HTTPStatus.NOT_FOUND), refused(HTTPStatus.NOT_FOUND, "no voice"))
    with pytest.raises(ElevenLabsError, match="no voice"):
        voices.speak(REQUEST.model_copy(update={"language_code": None}))


def test_speech_without_alignment_estimates_length_from_bitrate() -> None:
    speech = Speech(audio=b"x" * (MP3_BITS_PER_SECOND // 8))
    assert speech.duration_s == 1.0


def test_alignment_parses_api_shape() -> None:
    assert Alignment.model_validate(ALIGNED["alignment"]).characters == ["O", "i"]


def test_finish_without_ffmpeg_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_finish, "FFMPEG", "/nonexistent/ffmpeg")
    assert audio_finish.finish(b"mp3") is None


def test_finish_with_unreadable_audio_returns_none() -> None:
    if shutil.which(audio_finish.FFMPEG) is None:
        pytest.skip("ffmpeg is not installed here")
    assert audio_finish.finish(b"not audio") is None


def test_finish_adds_silence_after_speech() -> None:
    if shutil.which(audio_finish.FFMPEG) is None:
        pytest.skip("ffmpeg is not installed here")
    tone = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-f", "mp3", "pipe:1"],
        capture_output=True,
        check=True,
    ).stdout
    finished = audio_finish.finish(tone)
    assert finished is not None and len(finished) > len(tone)
