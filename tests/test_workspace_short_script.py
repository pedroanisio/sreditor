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
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

EPISODE = "/app/channels/funny-memes/episodes/1"
SHORT_SCRIPT = f"{EPISODE}/short-script"
FIELDS = {
    "duration_s": "5",
    "model": "v3",
    "voice_id": "v1",
    "language": "pt-BR",
    "block_count": "2",
    "text_count": "1",
    "block-0-start_s": "0",
    "block-0-end_s": "2",
    "block-0-function": "hook",
    "block-0-speech": "Oi.",
    "block-1-start_s": "2",
    "block-1-end_s": "5",
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
    # Each block is its own take, so the second block starts where the first take's audio ends.
    second_start = "0.3"  # "Oi." ends at 0.3 s, so the first take lasts that long
    assert owner.get(SHORT_SCRIPT).text.count(f'value="{second_start}"') == 2


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


def test_short_script_save_keeps_speaker_and_beats_set_through_the_api(
    owner: TestClient, rex: str
) -> None:
    beats = (
        f'[{{"at_s": 0, "character": "{rex}", "eyes": "wide", "mouth": "grin", "arms": "desk"}}]'
    )
    first = submit(owner, SHORT_SCRIPT, FIELDS | {"block-0-speaker": rex, "block-0-beats": beats})
    page = flash_after(owner, first)
    # Saving the page as rendered, untouched, must keep what only the API can set.
    flash_after(owner, submit(owner, SHORT_SCRIPT, page_fields(page)))
    script = owner.get("/channels/funny-memes/episodes/1/short-script").json()
    assert (
        script["blocks"][0]["speaker"] == rex and script["blocks"][0]["beats"][0]["mouth"] == "grin"
    )


def test_short_script_unreadable_beats_show_error_next_to_speech(owner: TestClient) -> None:
    response = submit(owner, SHORT_SCRIPT, FIELDS | {"block-0-beats": "{nope"})
    assert response.status_code == 422 and "could not be read" in response.text


def test_short_script_speaker_gone_from_library_shows_error_next_to_it(owner: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    response = submit(owner, SHORT_SCRIPT, FIELDS | {"block-1-speaker": missing})
    assert response.status_code == 422 and 'id="block-1-speaker-error"' in response.text


def page_fields(page: str) -> dict[str, str]:
    """The form fields as the page renders them, the way a browser would post them back."""
    fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page))
    fields |= dict(re.findall(r'<textarea[^>]*name="([^"]+)"[^>]*>([^<]*)</textarea>', page))
    for name, body in re.findall(
        r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', page, re.DOTALL
    ):
        chosen = re.search(r'<option value="([^"]*)"[^>]*selected', body)
        fields[name] = chosen.group(1) if chosen else ""
    return {name: html.unescape(value) for name, value in fields.items()}
