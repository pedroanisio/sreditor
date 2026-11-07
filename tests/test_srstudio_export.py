"""The srstudio zip of an episode, and the list of what it lacks (rules project.srstudio, project.export)."""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import zipfile
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor import short_script_operations, srstudio_timeline
from sreditor.deps import Services
from sreditor.elevenlabs import ElevenLabs
from sreditor.short_script import Block, BlockFunction
from sreditor.srstudio_timeline import direction_of
from sreditor.voice_markup import TtsModel
from tests.media_samples import MP3_TAGGED
from tests.test_api import FUNNY_MEMES
from tests.test_elevenlabs import KEY, VOICES, FakeTransport, ok

CHANNEL = "/channels/funny-memes"
EPISODE = f"{CHANNEL}/episodes/1"
ZIP = f"{EPISODE}/srstudio.zip"
HAS_FFMPEG = shutil.which("ffmpeg") is not None


def spoken(text: str, audio: bytes = MP3_TAGGED) -> tuple[int, bytes]:
    """An ElevenLabs answer for this text, a tenth of a second per character."""
    steps = range(len(text))
    return ok(
        {
            "audio_base64": base64.b64encode(audio).decode(),
            "alignment": {
                "characters": list(text),
                "character_start_times_seconds": [round(i * 0.1, 2) for i in steps],
                "character_end_times_seconds": [round((i + 1) * 0.1, 2) for i in steps],
            },
        }
    )


def created(client: TestClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(path, json=body)
    assert response.status_code == 201, response.text
    found: dict[str, Any] = response.json()
    return found


def saved(client: TestClient, path: str, body: dict[str, Any]) -> None:
    response = client.put(path, json=body)
    assert response.status_code == 200, response.text


@pytest.fixture(scope="module")
def take_audio(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """A real, decodable MP3 when ffmpeg is here, so the export can turn it into WAV."""
    if not HAS_FFMPEG:
        return MP3_TAGGED
    target = tmp_path_factory.mktemp("audio") / "tone.mp3"
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(target)]
    subprocess.run(command, check=True, timeout=60)
    return target.read_bytes()


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    created(client, f"{CHANNEL}/episodes", {"title": "Whose Milk"})
    return client


@pytest.fixture
def voice(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeTransport]:
    """Turn generation on with a fake ElevenLabs answering in order."""
    monkeypatch.setattr(short_script_operations, "finish", lambda audio: None)

    def answering(*answers: tuple[int, bytes]) -> FakeTransport:
        transport = FakeTransport(*answers)
        services: Services = app.state.services
        services.voice = ElevenLabs(KEY, "https://voice.test", transport)
        return transport

    return answering


@pytest.fixture
def rex(owner: TestClient) -> str:
    """Rex, a toon dog with an ElevenLabs voice and tuning on his sheet."""
    rex_id = str(created(owner, f"{CHANNEL}/characters", {"name": "Rex"})["id"])
    sheet = {
        "kind": "character",
        "world": "okayville",
        "character_kind": "animal",
        "rig": "toon-dog",
        "voice": {"voice_id": "dog1", "voice_name": "Charlie", "stability": 0.3, "speed": 1.1},
    }
    saved(owner, f"{CHANNEL}/characters/{rex_id}/srstudio", {"slug": "rex", "sheet": sheet})
    return rex_id


@pytest.fixture
def toon_style(owner: TestClient) -> None:
    """The episode draws in srstudio's toon style, whose sketches take beats."""
    style_id = str(created(owner, f"{CHANNEL}/styles", {"name": "Office"})["id"])
    sheet = {"kind": "style", "style": "toon"}
    saved(owner, f"{CHANNEL}/styles/{style_id}/srstudio", {"slug": "office", "sheet": sheet})
    saved(owner, f"{EPISODE}/style", {"style_id": style_id})


def script(rex: str) -> dict[str, Any]:
    """A narrator hook and a punch Rex says, with his beats."""
    beats = [
        {"at_s": 0, "character": rex, "eyes": "wide", "mouth": "grin", "arms": "desk"},
        {"at_s": 1, "character": rex, "eyes": "shock", "mouth": "o", "arms": "desk"},
    ]
    return {
        "duration_s": 5,
        "voice": {"voice_id": "v1", "model": "v3", "language": "pt-BR"},
        "blocks": [
            {"start_s": 0, "end_s": 2, "function": "hook", "speech": "Oi."},
            {
                "start_s": 2,
                "end_s": 5,
                "function": "punch",
                "speech": "[sigh] Tchau [pause:0.5] [emphasis]agora[/emphasis].",
                "speaker": rex,
                "beats": beats,
            },
        ],
    }


@pytest.fixture
def voiced(
    owner: TestClient,
    rex: str,
    toon_style: None,
    voice: Callable[..., FakeTransport],
    take_audio: bytes,
) -> FakeTransport:
    """The script saved and voiced: one take per block, then the voice names."""
    saved(owner, f"{EPISODE}/short-script", script(rex))
    said = spoken("[sighs] Tchau [pause] AGORA.", take_audio)
    transport = voice(spoken("Oi.", take_audio), said, ok(VOICES))
    assert owner.post(f"{EPISODE}/voice-over-takes").status_code == 201
    return transport


def test_generate_speaks_each_block_in_its_speakers_voice(voiced: FakeTransport) -> None:
    narrator, dog = voiced.sent[0], voiced.sent[1]
    tuning = (dog[3] or {})["voice_settings"]
    assert isinstance(tuning, dict)
    assert "/text-to-speech/v1/" in narrator[1] and "voice_settings" not in (narrator[3] or {})
    assert "/text-to-speech/dog1/" in dog[1]
    assert tuning["stability"] == 0.3


def test_generate_after_one_block_changed_speaks_only_that_block(
    owner: TestClient, rex: str, voiced: FakeTransport, voice: Callable[..., FakeTransport]
) -> None:
    changed = script(rex)
    changed["blocks"][0]["speech"] = "Olá."
    saved(owner, f"{EPISODE}/short-script", changed)
    transport = voice(spoken("Olá."), ok(VOICES))
    assert owner.post(f"{EPISODE}/voice-over-takes").status_code == 201
    assert len(transport.sent) == 2 and (transport.sent[0][3] or {})["text"] == "Olá."


def test_generate_character_without_voice_names_the_sheet(
    owner: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    mute = str(created(owner, f"{CHANNEL}/characters", {"name": "Mute"})["id"])
    body = {"duration_s": 2, "voice": {"voice_id": "v1"}, "blocks": [
        {"start_s": 0, "end_s": 2, "function": "hook", "speech": "Oi.", "speaker": mute}]}  # fmt: skip
    saved(owner, f"{EPISODE}/short-script", body)
    voice()
    response = owner.post(f"{EPISODE}/voice-over-takes")
    assert response.status_code == 422 and "srstudio sheet" in response.json()["detail"]


def test_export_without_voice_over_lists_what_is_missing(owner: TestClient) -> None:
    response = owner.get(ZIP)
    messages = [error["message"] for error in response.json()["errors"]]
    assert response.status_code == 409
    assert any("short script" in message for message in messages)
    assert any("visual style" in message for message in messages)


def test_export_after_script_changed_asks_for_new_voice_over(
    owner: TestClient, rex: str, voiced: FakeTransport
) -> None:
    changed = script(rex)
    changed["blocks"][0]["speech"] = "Olá."
    saved(owner, f"{EPISODE}/short-script", changed)
    assert "changed after the last voice-over" in owner.get(ZIP).json()["detail"]


def test_export_character_without_world_is_listed(owner: TestClient, voiced: FakeTransport) -> None:
    created(owner, f"{CHANNEL}/characters", {"name": "Nameless"})
    response = owner.get(ZIP)
    assert response.status_code == 409 and "Nameless has no srstudio world" in response.text


def test_export_beats_on_rig_that_is_no_toon_are_listed(
    owner: TestClient, rex: str, voiced: FakeTransport
) -> None:
    sheet = {"kind": "character", "world": "okayville", "character_kind": "animal",
             "rig": "laptop", "voice": {"voice_id": "dog1"}}  # fmt: skip
    path = f"{CHANNEL}/characters/{rex}/srstudio"
    owner.put(path, json={"slug": "rex", "sheet": sheet}, headers={"If-Match": '"1"'})
    assert "not a toon character" in owner.get(ZIP).text


def test_export_by_contributor_is_forbidden(
    app: FastAPI, owner: TestClient, sign_up: Callable[..., TestClient], mailer: Any
) -> None:
    from tests.test_episodes import joined

    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.get(ZIP).status_code == 403


def test_export_when_wav_cannot_be_made_names_the_block(
    owner: TestClient, voiced: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srstudio_timeline, "to_wav", lambda source, target: False)
    response = owner.get(ZIP)
    assert response.status_code == 409 and "block 1" in response.json()["detail"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg to turn takes into WAV")
def test_export_zip_holds_timeline_audio_bible_and_beats_but_no_waiver(
    owner: TestClient, voiced: FakeTransport
) -> None:
    response = owner.get(ZIP)
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(archive.namelist())
    assert names == {
        "episode.json", "timeline.json", "bible/cast.json", "bible/relationships.json",
        "assets/poses.json", "assets/events.json", "audio/take-01.wav", "audio/take-02.wav",
    }  # fmt: skip
    timeline = json.loads(archive.read("timeline.json"))
    first, second = timeline["items"]
    assert second["start"] == first["duration"] and second["actor"] == "rex"
    assert second["text"] == "Tchau agora." and [word["text"] for word in second["words"]] == [
        "Tchau",
        "AGORA",
    ]


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg to turn takes into WAV")
def test_export_bible_writes_empty_texts_as_open_questions(
    owner: TestClient, voiced: FakeTransport
) -> None:
    archive = zipfile.ZipFile(io.BytesIO(owner.get(ZIP).content))
    cast = json.loads(archive.read("bible/cast.json"))
    rex = cast["characters"][0]
    assert cast["schema"] == "1.0.0" and rex["slug"] == "rex"
    assert rex["look"]["drawing"] == [{"part": "toon-dog", "version": 1}]
    assert rex["who"]["job"].startswith("open:") and rex["voice"]["provider"] == "elevenlabs"


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg to turn takes into WAV")
def test_export_poses_show_the_toon_each_beat_draws(
    owner: TestClient, voiced: FakeTransport
) -> None:
    archive = zipfile.ZipFile(io.BytesIO(owner.get(ZIP).content))
    poses = json.loads(archive.read("assets/poses.json"))
    assert [appearance["who"] for appearance in poses] == ["dog"]
    assert [pose["eyes"] for pose in poses[0]["poses"]] == ["wide", "shock"]


def test_direction_keeps_known_tags_and_moves_others_to_quality() -> None:
    found = direction_of("[sigh] Tchau [slow]agora[/slow].", TtsModel.V3)
    assert found["tags"] == ["sighs"] and found["quality"] == "slowly"


def test_direction_finds_stressed_words_and_pauses() -> None:
    found = direction_of("Oi [pause:0.5] oi [emphasis]você[/emphasis].", TtsModel.V3)
    assert found["emphasis"] == ["você"]
    assert found["pauses"] == [{"after": "Oi", "seconds": 0.5, "nth": 0}]


def test_block_used_in_tests_is_valid() -> None:
    assert Block(start_s=0, end_s=1, function=BlockFunction.HOOK, speech="Oi.").speaker is None
