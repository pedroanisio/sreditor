"""The episode's short script page: writing blocks, warnings, roles and the voice-over panel."""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor import short_script_operations
from sreditor.deps import Services
from sreditor.elevenlabs import ElevenLabs
from tests.conftest import RecordingMailer
from tests.media_samples import MP3_TAGGED
from tests.test_elevenlabs import KEY, VOICES, FakeTransport, ok
from tests.workspace_helpers import autosave, flash_after, join, member, new_channel, submit

EPISODE = "/app/channels/funny-memes/episodes/1"
SHORT_SCRIPT = f"{EPISODE}/short-script"
FIELDS = {
    "duration_s": "5",
    "model": "v3",
    "voice_id": "v1",
    "language": "pt-BR",
    "block_count": "2",
    "text_count": "1",
    "block-0-duration_s": "2",
    "block-0-function": "hook",
    "block-0-speech": "Oi.",
    "block-1-duration_s": "3",
    "block-1-function": "punch",
    "block-1-speech": "Tchau.",
    "block-1-reaction": "laughter",
    "block-1-intensity": "3",
    "text-0-text": "Tchau",
    "text-0-shown": "throughout",
}


def spoken_answer(text: str) -> tuple[int, bytes]:
    """An ElevenLabs answer with audio and one alignment entry per character."""
    steps = range(len(text))
    return ok(
        {
            "audio_base64": base64.b64encode(MP3_TAGGED).decode(),
            "alignment": {
                "characters": list(text),
                "character_start_times_seconds": [round(i * 0.1, 2) for i in steps],
                "character_end_times_seconds": [round((i + 1) * 0.1, 2) for i in steps],
            },
        }
    )


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])  # shows and clears the note
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


def test_episode_page_links_to_short_script_tab(owner: TestClient) -> None:
    assert f'href="{SHORT_SCRIPT}"' in owner.get(EPISODE).text


def test_short_script_page_without_key_hides_generation(owner: TestClient) -> None:
    page = owner.get(SHORT_SCRIPT).text
    assert 'name="block-0-speech"' in page and "Generate voice-over" not in page


def test_short_script_save_shows_note_and_keeps_blocks(owner: TestClient) -> None:
    page = flash_after(owner, submit(owner, SHORT_SCRIPT, {**FIELDS, "revision": "0"}))
    assert "Changes saved." in page and "Tchau." in page
    assert "Worth a look before recording" not in page


def test_short_script_save_weak_script_shows_warning(owner: TestClient) -> None:
    page = flash_after(owner, submit(owner, SHORT_SCRIPT, {**FIELDS, "duration_s": "9"}))
    assert "Worth a look before recording" in page and "the video lasts 9" in page


def test_short_script_bad_tag_shows_error_next_to_speech(owner: TestClient) -> None:
    response = submit(owner, SHORT_SCRIPT, {**FIELDS, "block-0-speech": "Oi [shrug]."})
    assert response.status_code == 422 and 'id="block-0-speech-error"' in response.text
    assert "Oi [shrug]." in response.text


def test_short_script_stale_revision_shows_conflict(owner: TestClient) -> None:
    submit(owner, SHORT_SCRIPT, {**FIELDS, "revision": "0"})
    response = submit(owner, SHORT_SCRIPT, {**FIELDS, "revision": "0"})
    assert response.status_code == 409 and "Someone changed the short script" in response.text


def test_short_script_page_for_viewer_shows_script_without_form(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    submit(owner, SHORT_SCRIPT, FIELDS)
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(SHORT_SCRIPT).text
    assert "Tchau." in page and 'name="block-0-speech"' not in page


def test_short_script_page_with_key_lists_voices(
    owner: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(ok(VOICES))
    page = owner.get(SHORT_SCRIPT).text
    assert '<option value="v1"' in page and "Generate voice-over" in page


def test_short_script_generate_shows_take_and_applies_its_times(
    owner: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(spoken_answer("Oi."), spoken_answer("Tchau."), ok(VOICES))
    submit(owner, SHORT_SCRIPT, FIELDS)
    page = flash_after(owner, submit(owner, f"{SHORT_SCRIPT}/voice-over", {}))
    assert "Latest take" in page and "<audio" in page
    key = page.split('name="take_key" value="')[1].split('"')[0]
    applied = submit(owner, f"{SHORT_SCRIPT}/timings", {"take_key": key, "revision": "1"})
    assert applied.status_code == 303
    # Each block is its own take, so the second block starts where the first take's audio ends:
    # "Oi." ends at 0.3 s, so the first take lasts that long.
    assert "0.0–0.3 s" in owner.get(SHORT_SCRIPT).text


def test_short_script_generate_shows_the_voice_license_of_each_block(
    owner: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    voice(spoken_answer("Oi."), spoken_answer("Tchau."), ok(VOICES))
    submit(owner, SHORT_SCRIPT, FIELDS)
    submit(owner, f"{SHORT_SCRIPT}/voice-over", {})
    page = owner.get(SHORT_SCRIPT).text
    assert "Voice license" in page and "elevenlabs · Ana · eleven_v3 · plan creator" in page


def test_short_script_voice_license_in_portuguese_names_the_plan(
    owner: TestClient, voice: Callable[..., FakeTransport]
) -> None:
    transport = voice(spoken_answer("Oi."), spoken_answer("Tchau."), ok(VOICES))
    transport.plan_answer = (500, b"down")
    submit(owner, SHORT_SCRIPT, FIELDS)
    submit(owner, f"{SHORT_SCRIPT}/voice-over", {})
    page = owner.get(SHORT_SCRIPT, params={"lang": "pt-BR"}).text
    assert "Licença da voz" in page and "plano desconhecido" in page


@pytest.fixture
def rex(owner: TestClient) -> str:
    """Rex, a character of the library, by id."""
    owner.get(
        submit(owner, "/app/channels/funny-memes/library/characters", {"name": "Rex"}).headers[
            "location"
        ]
    )
    page = owner.get("/app/channels/funny-memes/library").text
    return page.split('href="/app/channels/funny-memes/library/characters/')[1].split("/")[0]


def test_short_script_page_offers_library_characters_as_speakers(
    owner: TestClient, rex: str
) -> None:
    page = owner.get(SHORT_SCRIPT).text
    assert f'<option value="{rex}"' in page and "The narrator" in page


def beat_fields(rex: str, **changes: str | list[str]) -> dict[str, str | list[str]]:
    """Block 0 spoken by Rex with one beat, plus the blank row the page always adds."""
    fields: dict[str, str | list[str]] = {
        "block-0-speaker": rex,
        "block-0-beat_count": "2",
        "block-0-beat-0-at_s": "0",
        "block-0-beat-0-character": rex,
        "block-0-beat-0-eyes": "wide",
        "block-0-beat-0-mouth": "grin",
        "block-0-beat-0-arms": "desk",
        "block-0-beat-1-character": "",
    }
    return fields | {f"block-0-beat-0-{name}": value for name, value in changes.items()}


def stored_beats(owner: TestClient) -> list[dict[str, object]]:
    script = owner.get("/channels/funny-memes/episodes/1/short-script").json()
    beats: list[dict[str, object]] = script["blocks"][0]["beats"]
    return beats


def test_short_script_page_offers_beats_for_library_characters(owner: TestClient, rex: str) -> None:
    page = owner.get(SHORT_SCRIPT).text
    assert 'name="block-0-beat-0-character"' in page and 'name="block-0-beat-0-eyes"' in page


def test_short_script_page_without_characters_hides_beats(owner: TestClient) -> None:
    assert "beat-0-character" not in owner.get(SHORT_SCRIPT).text


def test_short_script_save_stores_beats_and_drops_blank_row(owner: TestClient, rex: str) -> None:
    fields = beat_fields(rex, wear=["glasses", "sweat"])
    flash_after(owner, submit(owner, SHORT_SCRIPT, FIELDS | fields))
    beats = stored_beats(owner)
    assert len(beats) == 1 and beats[0]["mouth"] == "grin"
    assert beats[0]["wear"] == ["glasses", "sweat"]


def test_short_script_save_untouched_page_keeps_speaker_and_beats(
    owner: TestClient, rex: str
) -> None:
    page = flash_after(owner, submit(owner, SHORT_SCRIPT, FIELDS | beat_fields(rex, wear="flush")))
    flash_after(owner, submit(owner, SHORT_SCRIPT, page_fields(page)))
    script = owner.get("/channels/funny-memes/episodes/1/short-script").json()
    assert script["blocks"][0]["speaker"] == rex
    assert [beat["wear"] for beat in stored_beats(owner)] == [["flush"]]


def test_short_script_first_beat_after_zero_shows_error_in_open_beats(
    owner: TestClient, rex: str
) -> None:
    response = submit(owner, SHORT_SCRIPT, FIELDS | beat_fields(rex, at_s="0.5"))
    assert (
        response.status_code == 422 and "The first beat of a block starts at 0 s." in response.text
    )
    assert re.search(r'<details class="more" open>\s*<summary[^>]*>.*?Beats', response.text)


def test_short_script_beat_action_without_prop_shows_error_next_to_prop(
    owner: TestClient, rex: str
) -> None:
    response = submit(owner, SHORT_SCRIPT, FIELDS | beat_fields(rex, action="open"))
    assert response.status_code == 422 and 'id="block-0-beat-0-prop-error"' in response.text


def test_short_script_beat_without_time_shows_error_next_to_it(owner: TestClient, rex: str) -> None:
    response = submit(owner, SHORT_SCRIPT, FIELDS | beat_fields(rex, at_s=""))
    assert response.status_code == 422 and 'id="block-0-beat-0-at_s-error"' in response.text


def test_short_script_beat_with_unknown_pose_shows_error_next_to_it(
    owner: TestClient, rex: str
) -> None:
    response = submit(owner, SHORT_SCRIPT, FIELDS | beat_fields(rex, eyes="sideways"))
    assert response.status_code == 422 and 'id="block-0-beat-0-eyes-error"' in response.text


def test_short_script_speaker_gone_from_library_shows_error_next_to_it(owner: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    response = submit(owner, SHORT_SCRIPT, FIELDS | {"block-1-speaker": missing})
    assert response.status_code == 422 and 'id="block-1-speaker-error"' in response.text


def page_fields(page: str) -> dict[str, str]:
    """The form fields as the page renders them, the way a browser would post them back."""
    inputs = re.findall(r"<input[^>]*>", page)
    fields = dict(
        re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', tag)[0]
        for tag in inputs
        if 'type="checkbox"' not in tag and re.search(r'name="[^"]+"[^>]*value=', tag)
    )
    for tag in inputs:
        if 'type="checkbox"' in tag and " checked" in tag:
            name, value = re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', tag)[0]
            fields[name] = f"{fields[name]},{value}" if name in fields else value
    fields |= dict(re.findall(r'<textarea[^>]*name="([^"]+)"[^>]*>([^<]*)</textarea>', page))
    for name, body in re.findall(
        r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', page, re.DOTALL
    ):
        chosen = re.search(r'<option value="([^"]*)"[^>]*selected', body)
        fields[name] = chosen.group(1) if chosen else ""
    return {name: html.unescape(value) for name, value in fields.items()}


def test_short_script_rows_show_chained_times(owner: TestClient) -> None:
    page = flash_after(owner, submit(owner, SHORT_SCRIPT, {**FIELDS, "revision": "0"}))
    assert "0.0–2.0 s" in page and "2.0–5.0 s" in page


def test_short_script_blank_duration_is_estimated_from_words(owner: TestClient) -> None:
    flash_after(owner, submit(owner, SHORT_SCRIPT, FIELDS | {"block-0-duration_s": ""}))
    script = owner.get("/channels/funny-memes/episodes/1/short-script").json()
    assert script["blocks"][0]["end_s"] == 1 and script["blocks"][1]["start_s"] == 1


def test_short_script_zero_duration_shows_error_next_to_it(owner: TestClient) -> None:
    response = submit(owner, SHORT_SCRIPT, FIELDS | {"block-1-duration_s": "0"})
    assert response.status_code == 422 and 'id="block-1-duration_s-error"' in response.text


def test_short_script_add_block_shows_new_row_without_saving(owner: TestClient) -> None:
    response = submit(owner, SHORT_SCRIPT, FIELDS | {"action": "add-block-0"})
    assert response.status_code == 200 and 'id="block-1"' in response.text
    assert 'class="script-row is-new" id="block-1"' in response.text
    assert owner.get("/channels/funny-memes/episodes/1/short-script").json()["revision"] == 0


def test_short_script_remove_block_saves_without_it(owner: TestClient) -> None:
    flash_after(owner, submit(owner, SHORT_SCRIPT, FIELDS | {"action": "remove-block-0"}))
    script = owner.get("/channels/funny-memes/episodes/1/short-script").json()
    assert [block["speech"] for block in script["blocks"]] == ["Tchau."]


def test_short_script_move_block_up_saves_new_order(owner: TestClient) -> None:
    flash_after(owner, submit(owner, SHORT_SCRIPT, FIELDS | {"action": "up-block-1"}))
    script = owner.get("/channels/funny-memes/episodes/1/short-script").json()
    assert [block["speech"] for block in script["blocks"]] == ["Tchau.", "Oi."]


def test_short_script_add_text_on_empty_list_shows_a_row(owner: TestClient) -> None:
    sent = FIELDS | {"text_count": "0", "action": "add-text--1"}
    response = submit(owner, SHORT_SCRIPT, sent)
    assert response.status_code == 200 and 'name="text-0-text"' in response.text


def test_short_script_autosave_answers_new_revision(owner: TestClient) -> None:
    response = autosave(owner, SHORT_SCRIPT, {**FIELDS, "revision": "0"})
    assert response.status_code == 204 and response.headers["Sreditor-Revision"] == "1"


def test_short_script_autosave_with_error_answers_its_status(owner: TestClient) -> None:
    response = autosave(owner, SHORT_SCRIPT, FIELDS | {"block-0-duration_s": "x"})
    assert response.status_code == 422 and not response.content


def test_short_script_speech_in_other_language_than_voice_warns(owner: TestClient) -> None:
    english = "This is the story of a dog and the day it would not go to work with you."
    page = flash_after(owner, submit(owner, SHORT_SCRIPT, FIELDS | {"block-0-speech": english}))
    assert "looks written in English" in page


def test_short_script_voice_summary_names_model_and_language(owner: TestClient) -> None:
    page = flash_after(owner, submit(owner, SHORT_SCRIPT, FIELDS))
    assert "Voice: v1 · v3" in page and "voice on their srstudio sheet" in page


def test_short_script_move_block_down_carries_its_beats(owner: TestClient, rex: str) -> None:
    fields = FIELDS | beat_fields(rex) | {"action": "down-block-0"}
    flash_after(owner, submit(owner, SHORT_SCRIPT, fields))
    script = owner.get("/channels/funny-memes/episodes/1/short-script").json()
    first, second = script["blocks"]
    assert first["speech"] == "Tchau." and first["beats"] == []
    assert second["speaker"] == rex and second["beats"][0]["mouth"] == "grin"


def test_short_script_add_block_above_renumbers_beats_with_their_block(
    owner: TestClient, rex: str
) -> None:
    page = submit(owner, SHORT_SCRIPT, FIELDS | beat_fields(rex) | {"action": "add-block--1"}).text
    assert re.search(
        rf'name="block-1-beat-0-character"[^>]*>\s*(<option[^>]*>[^<]*</option>\s*)*?<option value="{rex}" selected',
        page,
    )
