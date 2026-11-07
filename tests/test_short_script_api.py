"""Short scripts and generated voice-overs through the API and MCP, with a fake ElevenLabs."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from http import HTTPStatus
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor import short_script_operations
from sreditor.block_takes import BlockTakeRecord, BlockTakeStore
from sreditor.channels import NarratorVoice
from sreditor.deps import Services
from sreditor.elevenlabs import ElevenLabs
from sreditor.settings import AppSettings
from sreditor.short_script import ShortScriptInput
from sreditor.short_script_operations import ScriptNotReady, narrator_voice
from tests.conftest import RecordingMailer
from tests.media_samples import MP3_TAGGED
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_elevenlabs import KEY, VOICES, FakeTransport, ok, refused
from tests.test_episodes import joined

EPISODE = "/channels/funny-memes/episodes/1"
SHORT_SCRIPT = f"{EPISODE}/short-script"
TAKES = f"{EPISODE}/voice-over-takes"
TIMINGS = f"{SHORT_SCRIPT}/timings"
SPOKEN = "Oi. Tchau."
SCRIPT = {
    "duration_s": 5,
    "voice": {"voice_id": "v1", "model": "v3", "language": "pt-BR"},
    "blocks": [
        {"start_s": 0, "end_s": 2, "function": "hook", "speech": "Oi."},
        {
            "start_s": 2,
            "end_s": 5,
            "function": "punch",
            "speech": "Tchau.",
            "impact": {"reaction": "laughter", "intensity": 3},
        },
    ],
}


def spoken(text: str = SPOKEN, step: float = 0.1) -> dict[str, Any]:
    """An ElevenLabs answer with audio and one alignment entry per character."""
    return {
        "audio_base64": base64.b64encode(MP3_TAGGED).decode(),
        "alignment": {
            "characters": list(text),
            "character_start_times_seconds": [round(i * step, 2) for i in range(len(text))],
            "character_end_times_seconds": [round((i + 1) * step, 2) for i in range(len(text))],
        },
    }


# One ElevenLabs answer per block: each block is its own take (rule project.voice-generation).
BLOCK_ANSWERS = (ok(spoken("Oi.")), ok(spoken("Tchau.")))
# Each take lasts until its last character, longer than the tiny sample MP3: "Oi." 0.3 s, "Tchau." 0.6 s.
FIRST_TAKE_SECONDS = 0.3
BOTH_TAKES_SECONDS = 0.9


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def voice(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeTransport]:
    """Turn generation on with a fake ElevenLabs answering the given responses in order."""
    monkeypatch.setattr(short_script_operations, "finish", lambda audio: None)

    def answering(*answers: tuple[int, bytes]) -> FakeTransport:
        transport = FakeTransport(*answers)
        services: Services = app.state.services
        services.voice = ElevenLabs(KEY, "https://voice.test", transport)
        return transport

    return answering


@pytest.fixture
def written(owner: TestClient) -> TestClient:
    assert owner.put(SHORT_SCRIPT, json=SCRIPT).status_code == 200
    return owner


# Script


def test_short_script_get_unwritten_has_etag_zero(owner: TestClient) -> None:
    response = owner.get(SHORT_SCRIPT)
    assert response.headers["etag"] == '"0"' and response.json()["blocks"] == []


def test_short_script_put_returns_new_etag_without_warnings(owner: TestClient) -> None:
    response = owner.put(SHORT_SCRIPT, json=SCRIPT, headers={"If-Match": '"0"'})
    assert response.headers["etag"] == '"1"' and response.json()["warnings"] == []


def test_short_script_put_weak_script_saves_with_warnings(owner: TestClient) -> None:
    weak = {**SCRIPT, "duration_s": 9}
    response = owner.put(SHORT_SCRIPT, json=weak)
    assert response.status_code == 200
    assert [item["kind"] for item in response.json()["warnings"]] == ["ends_early"]


def test_short_script_put_unknown_tag_returns_field_error(owner: TestClient) -> None:
    broken = {**SCRIPT, "blocks": [{**SCRIPT["blocks"][0], "speech": "A [shrug]."}]}
    response = owner.put(SHORT_SCRIPT, json=broken)
    assert response.status_code == 422 and "[shrug]" in response.text


def test_short_script_put_stale_etag_returns_precondition_failed(written: TestClient) -> None:
    response = written.put(SHORT_SCRIPT, json=SCRIPT, headers={"If-Match": '"0"'})
    assert response.status_code == 412 and response.headers["etag"] == '"1"'


def test_short_script_put_by_viewer_is_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.get(SHORT_SCRIPT).status_code == 200
    assert viewer.put(SHORT_SCRIPT, json=SCRIPT).status_code == 403


def test_short_script_write_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, owner: TestClient, app_settings: AppSettings
) -> None:
    block(app_settings, "/", "short_script.write")
    response = agent(app, new_token(owner)).put(SHORT_SCRIPT, json=SCRIPT)
    assert response.status_code == 403 and response.json()["decision"] == "BLOCK"


# Voices and generation


def test_voices_without_key_returns_service_unavailable(owner: TestClient) -> None:
    response = owner.get("/channels/funny-memes/voices")
    assert response.status_code == 503
    assert response.json()["type"].endswith("voice-generation-off")


def test_voices_lists_elevenlabs_voices(
    owner: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(ok(VOICES))
    names = [item["name"] for item in owner.get("/channels/funny-memes/voices").json()]
    assert names == ["Ana", "zoe"]


def test_generate_without_key_returns_service_unavailable(written: TestClient) -> None:
    assert written.post(TAKES).status_code == 503


def test_generate_without_voice_returns_unprocessable(
    owner: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice()
    owner.put(SHORT_SCRIPT, json={**SCRIPT, "voice": {"voice_id": ""}})
    response = owner.post(TAKES)
    assert response.status_code == 422 and "Choose a voice" in response.json()["detail"]


def test_generate_adds_voice_over_file_with_measured_times(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES))
    response = written.post(TAKES)
    take = response.json()
    assert response.status_code == 201 and not take["is_reused"]
    assert take["suggested"] == [
        {"start_s": 0.0, "end_s": FIRST_TAKE_SECONDS},
        {"start_s": FIRST_TAKE_SECONDS, "end_s": BOTH_TAKES_SECONDS},
    ]
    files = written.get(f"{EPISODE}/media").json()["items"]
    assert [(item["kind"], item["name"]) for item in files] == [("voice_over", "Ana · v3")]


def test_generate_same_script_again_reuses_take_without_calling(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    transport = voice(*BLOCK_ANSWERS, ok(VOICES))
    first = written.post(TAKES).json()
    again = written.post(TAKES).json()
    assert again["is_reused"] and again["media_id"] == first["media_id"]
    assert len(transport.sent) == len(BLOCK_ANSWERS) + 1


def test_generate_after_file_deleted_makes_new_take(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES), ok(VOICES))
    first = written.post(TAKES).json()
    assert written.delete(f"{EPISODE}/media/{first['media_id']}").status_code == 204
    again = written.post(TAKES).json()
    assert not again["is_reused"] and again["media_id"] != first["media_id"]


def test_generate_refused_by_elevenlabs_returns_bad_gateway(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(refused(HTTPStatus.UNAUTHORIZED, "invalid key"))
    response = written.post(TAKES)
    assert response.status_code == 502 and "invalid key" in response.json()["detail"]


def test_generate_by_viewer_is_forbidden(
    written: TestClient,
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
    voice: Callable[..., FakeTransport],
) -> None:
    voice()
    viewer = joined(written, sign_up, mailer, "viewer")
    assert viewer.post(TAKES).status_code == 403


# Timings


def test_apply_timings_replaces_block_times_and_length(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES))
    key = written.post(TAKES).json()["key"]
    response = written.post(TIMINGS, json={"take_key": key}, headers={"If-Match": '"1"'})
    script = response.json()
    assert response.headers["etag"] == '"2"' and script["duration_s"] == BOTH_TAKES_SECONDS
    assert [item["start_s"] for item in script["blocks"]] == [0.0, FIRST_TAKE_SECONDS]


def test_apply_timings_after_blocks_changed_returns_conflict(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES))
    key = written.post(TAKES).json()["key"]
    written.put(SHORT_SCRIPT, json={**SCRIPT, "blocks": SCRIPT["blocks"][:1]})
    assert written.post(TIMINGS, json={"take_key": key}).status_code == 409


def test_apply_timings_of_unknown_take_returns_not_found(written: TestClient) -> None:
    assert written.post(TIMINGS, json={"take_key": "0" * 16}).status_code == 404


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_write_short_script_then_get_returns_blocks(mcp: TestClient) -> None:
    tool(mcp, "write_short_script", {"slug": "funny-memes", "number": 1, "short_script": SCRIPT})
    found = tool(mcp, "get_short_script", {"slug": "funny-memes", "number": 1})
    assert len(found["structuredContent"]["short_script"]["blocks"]) == 2


def test_mcp_generate_voice_over_returns_take(
    mcp: TestClient, written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES))
    result = tool(mcp, "generate_voice_over", {"slug": "funny-memes", "number": 1})
    assert result["structuredContent"]["take"]["duration_s"] == BOTH_TAKES_SECONDS


def test_mcp_generate_voice_over_replay_is_marked_and_spends_nothing_more(
    mcp: TestClient, written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    transport = voice(*BLOCK_ANSWERS, ok(VOICES))
    arguments = {"slug": "funny-memes", "number": 1, "idempotency_key": "speak-1"}
    first = tool(mcp, "generate_voice_over", arguments)
    calls = len(transport.sent)
    again = tool(mcp, "generate_voice_over", arguments)
    assert first["_meta"] == {"sreditor/replayed": False}
    assert again["_meta"] == {"sreditor/replayed": True}
    assert len(transport.sent) == calls


def test_mcp_generate_voice_over_off_names_the_setting(
    mcp: TestClient, written: TestClient
) -> None:
    result = tool(mcp, "generate_voice_over", {"slug": "funny-memes", "number": 1})
    assert result["isError"] and "SREDITOR_ELEVENLABS_API_KEY" in result["content"][0]["text"]


def test_mcp_list_voices_returns_voices(
    mcp: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(ok(VOICES))
    result = tool(mcp, "list_voices", {"slug": "funny-memes"})
    assert [item["voice_id"] for item in result["structuredContent"]["voices"]] == ["v1", "v2"]


# Estimate, words per take, license and the brand's narrator (rule project.voice.estimate)

ESTIMATE = f"{EPISODE}/voice-over/estimate"
VOICE_OVER = f"{EPISODE}/voice-over"
BRAND_NARRATOR = {
    "voice_id": "brand1",
    "voice_name": "Brand Voice",
    "tuning": {
        "stability": 0.4,
        "similarity_boost": 0.8,
        "style": 0.1,
        "speed": 1.0,
        "use_speaker_boost": True,
    },
}
BYE = "Adeus."


def speak_requests(transport: FakeTransport) -> list[tuple[str, dict[str, Any]]]:
    """The text-to-speech requests sent, each as its url and body."""
    return [(url, body or {}) for _, url, _, body in transport.sent if "/v1/text-to-speech/" in url]


def test_estimate_unspoken_script_counts_every_block_as_new(written: TestClient) -> None:
    estimate = written.get(ESTIMATE).json()
    blocks = [
        (item["speaker"], item["characters"], item["is_reused"]) for item in estimate["blocks"]
    ]
    assert blocks == [("narrator", 3, False), ("narrator", 6, False)]
    assert estimate["new_characters"] == 9 and estimate["estimated_credits"] == 9
    assert estimate["model_id"] == "eleven_v3" and estimate["credits_per_character"] == 1.0


def test_estimate_counts_the_speech_without_voice_tags(written: TestClient) -> None:
    tagged = {**SCRIPT["blocks"][0], "speech": "[pause:0.5] Oi [irony]tu[/irony]."}
    written.put(SHORT_SCRIPT, json={**SCRIPT, "blocks": [tagged]})
    assert written.get(ESTIMATE).json()["blocks"][0]["characters"] == len("Oi tu.")


def test_estimate_after_generation_reuses_every_block(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES))
    written.post(TAKES)
    estimate = written.get(ESTIMATE).json()
    assert all(item["is_reused"] for item in estimate["blocks"])
    assert estimate["new_characters"] == 0 and estimate["estimated_credits"] == 0
    assert estimate["reused_characters"] == 9


def test_estimate_block_repeating_an_earlier_block_is_reused(written: TestClient) -> None:
    twice = {
        **SCRIPT,
        "blocks": [SCRIPT["blocks"][0], {**SCRIPT["blocks"][0], "start_s": 2, "end_s": 4}],
    }
    written.put(SHORT_SCRIPT, json=twice)
    estimate = written.get(ESTIMATE).json()
    assert [item["is_reused"] for item in estimate["blocks"]] == [False, True]
    assert estimate["new_characters"] == 3


def test_estimate_without_speech_returns_unprocessable(owner: TestClient) -> None:
    owner.put(SHORT_SCRIPT, json={**SCRIPT, "blocks": []})
    assert owner.get(ESTIMATE).status_code == 422


def test_estimate_without_voice_names_the_missing_voice(owner: TestClient) -> None:
    owner.put(SHORT_SCRIPT, json={**SCRIPT, "voice": {"voice_id": ""}})
    response = owner.get(ESTIMATE)
    assert response.status_code == 422 and "Choose a voice" in response.json()["detail"]


def test_generate_after_edit_spends_only_the_characters_the_estimate_counted(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    transport = voice(*BLOCK_ANSWERS, ok(VOICES), ok(spoken(BYE)))
    written.post(TAKES)
    edited = {**SCRIPT["blocks"][1], "speech": BYE}
    written.put(SHORT_SCRIPT, json={**SCRIPT, "blocks": [SCRIPT["blocks"][0], edited]})
    before = len(speak_requests(transport))
    estimate = written.get(ESTIMATE).json()
    written.post(TAKES)
    spent = speak_requests(transport)[before:]
    assert estimate["new_characters"] == len(BYE) == sum(len(body["text"]) for _, body in spent)
    assert len(spent) == 1


def test_get_voice_over_before_generating_returns_not_found(written: TestClient) -> None:
    assert written.get(VOICE_OVER).status_code == 404


def test_get_voice_over_times_words_from_the_take_and_the_joined_audio(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES))
    written.post(TAKES)
    first, second = written.get(VOICE_OVER).json()["block_takes"]
    assert (first["block_number"], first["start_s"], first["end_s"]) == (1, 0.0, 0.2)
    assert second["words"] == [{"text": "Tchau", "start": 0.0, "end": 0.5}]
    assert second["offset_s"] == FIRST_TAKE_SECONDS
    assert second["joined_words"] == [{"text": "Tchau", "start": 0.3, "end": 0.8}]
    assert (second["joined_start_s"], second["joined_end_s"]) == (0.3, 0.8)


def test_generate_returns_each_block_take_with_its_license(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES))
    licensed = written.post(TAKES).json()["block_takes"][0]["license"]
    assert (licensed["provider"], licensed["voice_id"], licensed["voice_name"]) == (
        "elevenlabs",
        "v1",
        "Ana",
    )
    assert (licensed["model"], licensed["plan"]) == ("eleven_v3", "creator")


def test_generate_when_the_plan_cannot_be_read_records_unknown(
    written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    transport = voice(*BLOCK_ANSWERS, ok(VOICES))
    transport.plan_answer = refused(HTTPStatus.FORBIDDEN)
    assert written.post(TAKES).json()["block_takes"][0]["license"]["plan"] == "unknown"


def test_block_takes_made_before_licenses_get_one_with_unknown_plan(
    app: FastAPI, written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES))
    written.post(TAKES)
    key = written.get(VOICE_OVER).json()["block_takes"][0]["key"]
    store = app.state.services.block_takes
    with store.database.connect() as connection:
        connection.execute("UPDATE voice_block_takes SET license = NULL")
    reopened = BlockTakeStore(store.database)
    found = reopened.find(BlockTakeRecord(channel="funny-memes", number=1, key=key))
    assert found is not None and found.license is not None
    assert (found.license.plan, found.license.voice_id) == ("unknown", "v1")
    assert (found.license.model, found.license.created_at) == ("eleven_v3", found.created_at)


def set_brand_narrator(owner: TestClient) -> None:
    channel = owner.get("/channels/funny-memes").json()
    channel["brand"]["narrator_voice"] = BRAND_NARRATOR
    assert owner.put("/channels/funny-memes", json=channel).status_code == 200


def test_generate_without_script_voice_narrates_with_the_brand_voice(
    owner: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    set_brand_narrator(owner)
    owner.put(SHORT_SCRIPT, json={**SCRIPT, "voice": {"voice_id": ""}})
    transport = voice(*BLOCK_ANSWERS)
    taken = owner.post(TAKES).json()
    url, body = speak_requests(transport)[0]
    assert "/v1/text-to-speech/brand1/" in url and body["voice_settings"]["stability"] == 0.4
    assert taken["voice_id"] == "brand1"
    assert taken["block_takes"][0]["license"]["voice_name"] == "Brand Voice"


def test_generate_with_script_voice_ignores_the_brand_voice(
    owner: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    set_brand_narrator(owner)
    owner.put(SHORT_SCRIPT, json=SCRIPT)
    transport = voice(*BLOCK_ANSWERS, ok(VOICES))
    owner.post(TAKES)
    assert "/v1/text-to-speech/v1/" in speak_requests(transport)[0][0]


def test_estimate_without_script_voice_names_the_brand_voice(owner: TestClient) -> None:
    set_brand_narrator(owner)
    owner.put(SHORT_SCRIPT, json={**SCRIPT, "voice": {"voice_id": ""}})
    blocks = owner.get(ESTIMATE).json()["blocks"]
    assert {(item["voice_id"], item["voice_name"]) for item in blocks} == {
        ("brand1", "Brand Voice")
    }


def test_narrator_voice_script_voice_wins_over_the_brand_default() -> None:
    chosen = ShortScriptInput.model_validate({"voice": {"voice_id": "own"}})
    default = NarratorVoice.model_validate(BRAND_NARRATOR)
    assert narrator_voice(chosen, default).voice_id == "own"


def test_narrator_voice_without_either_voice_is_not_ready() -> None:
    with pytest.raises(ScriptNotReady):
        narrator_voice(ShortScriptInput(), None)


def test_mcp_estimate_voice_over_returns_totals(mcp: TestClient, written: TestClient) -> None:
    result = tool(mcp, "estimate_voice_over", {"slug": "funny-memes", "number": 1})
    content = result["structuredContent"]
    assert content["new_characters"] == 9 and content["estimated_credits"] == 9


def test_mcp_estimate_voice_over_without_speech_names_the_fix(mcp: TestClient) -> None:
    result = tool(mcp, "estimate_voice_over", {"slug": "funny-memes", "number": 1})
    assert result["isError"] and "write_short_script" in result["content"][0]["text"]


def test_mcp_get_voice_over_before_generating_names_the_fix(
    mcp: TestClient, written: TestClient
) -> None:
    result = tool(mcp, "get_voice_over", {"slug": "funny-memes", "number": 1})
    assert result["isError"] and "generate_voice_over" in result["content"][0]["text"]


def test_mcp_get_voice_over_returns_words_per_block(
    mcp: TestClient, written: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(*BLOCK_ANSWERS, ok(VOICES))
    tool(mcp, "generate_voice_over", {"slug": "funny-memes", "number": 1})
    read = tool(mcp, "get_voice_over", {"slug": "funny-memes", "number": 1})
    blocks = read["structuredContent"]["take"]["block_takes"]
    assert [item["joined_words"][0]["text"] for item in blocks] == ["Oi", "Tchau"]
