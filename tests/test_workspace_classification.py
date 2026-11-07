"""The classification pages: an episode's classification tab and the channel's profile."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.web.workspace.classification_form import field_of, group_of
from tests.conftest import RecordingMailer
from tests.test_agentic import agent, new_token
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

TAB = "/app/channels/funny-memes/episodes/1/classification"
PROFILE = "/app/channels/funny-memes/profile"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    created = submit(client, "/app/channels/funny-memes/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])  # shows and clears the note
    return client


def test_field_of_dimension_location_is_dimension() -> None:
    assert field_of(["dimensions", "genres", "items"]) == "genres"


def test_field_of_satire_locations_are_satire_fields() -> None:
    assert field_of(["satire"]) == "satire_target"
    assert field_of(["satire", "criticized_behavior"]) == "satire_behavior"


def test_field_of_empty_location_is_form() -> None:
    assert field_of([]) == "form"


def test_group_of_unknown_key_is_first_group() -> None:
    assert group_of("nope").key == "purpose"


def test_classification_tab_shows_every_group(owner: TestClient) -> None:
    page = owner.get(TAB)
    assert page.status_code == 200
    for title in ("Who it is for and why", "Genre and approach", "Look"):
        assert title in page.text
    assert "Humor devices" in page.text and "Not classified yet" in page.text


def test_classification_save_keeps_values_and_says_saved(owner: TestClient) -> None:
    sent = {
        "group": "tone",
        "tones": "ironic",
        "tones_also": ["playful", "ironic"],
        "revision": "0",
    }
    page = flash_after(owner, submit(owner, TAB, sent))
    assert "Changes saved." in page
    assert '<span class="tag">Ironic</span>' in page
    assert '<span class="tag outline">Playful</span>' in page


def test_classification_group_save_keeps_other_groups(owner: TestClient) -> None:
    submit(owner, TAB, {"group": "tone", "tones": "warm", "revision": "0"})
    submit(owner, TAB, {"group": "look", "visual_techniques": "animation_2d", "revision": "1"})
    page = owner.get(TAB).text
    assert "Warm" in page and "2D animation" in page


def test_classification_three_secondary_values_is_field_error(owner: TestClient) -> None:
    sent = {"group": "tone", "tones": "warm", "tones_also": ["serious", "ironic", "absurd"]}
    response = submit(owner, TAB, {**sent, "revision": "0"})
    assert response.status_code == 422
    assert 'id="tones-error"' in response.text


def test_classification_comedy_without_device_shows_error_next_to_field(
    owner: TestClient,
) -> None:
    response = submit(owner, TAB, {"group": "genre", "genres": "comedy", "revision": "0"})
    assert response.status_code == 422
    assert "Comedy needs at least one humor device" in response.text
    assert 'id="humor_devices-error"' in response.text


def test_classification_unknown_state_is_field_error(owner: TestClient) -> None:
    response = submit(owner, TAB, {"group": "tone", "tones": "state:bogus", "revision": "0"})
    assert response.status_code == 422
    assert 'id="tones-error"' in response.text


def test_classification_satire_without_approach_shows_on_target(owner: TestClient) -> None:
    sent = {"group": "genre", "satire_target": "Banks", "satire_behavior": "Fees", "revision": "0"}
    response = submit(owner, TAB, sent)
    assert 'id="satire_target-error"' in response.text


def test_classification_satirical_without_target_opens_satire_fields(owner: TestClient) -> None:
    sent = {"group": "genre", "approaches": "satirical", "revision": "0"}
    response = submit(owner, TAB, sent)
    assert response.status_code == 422
    assert 'id="satire_target-error"' in response.text


def test_classification_satire_fields_hidden_without_satirical(owner: TestClient) -> None:
    assert 'name="satire_target"' not in owner.get(TAB).text


def test_classification_stale_revision_is_conflict(owner: TestClient) -> None:
    submit(owner, TAB, {"group": "tone", "tones": "warm", "revision": "0"})
    response = submit(owner, TAB, {"group": "tone", "tones": "serious", "revision": "0"})
    assert response.status_code == 409
    assert "Reload the page" in response.text


def test_classification_agent_proposal_confirmed_by_person_save(
    app: FastAPI, owner: TestClient
) -> None:
    body = {
        "method": "ai",
        "dimensions": {
            "tones": {"state": "present", "items": [{"value": "warm", "confidence": 0.6}]}
        },
    }
    api = agent(app, new_token(owner))
    assert api.put("/channels/funny-memes/episodes/1/classification", json=body).status_code == 200
    page = owner.get(TAB).text
    assert "An agent proposed this classification" in page and "Warm · 60%" in page
    flash_after(owner, submit(owner, TAB, {"group": "tone", "tones": "warm", "revision": "1"}))
    found = api.get("/channels/funny-memes/episodes/1/classification").json()
    assert found["is_reviewed"] is True
    assert found["classification"]["dimensions"]["tones"]["items"][0]["confidence"] == 0.6


def test_classification_confirm_as_is_marks_reviewed(app: FastAPI, owner: TestClient) -> None:
    body = {
        "method": "ai",
        "dimensions": {
            "tones": {"state": "present", "items": [{"value": "warm", "confidence": 1}]}
        },
    }
    api = agent(app, new_token(owner))
    api.put("/channels/funny-memes/episodes/1/classification", json=body)
    page = flash_after(owner, submit(owner, TAB + "/confirm", {"revision": "1"}))
    assert "An agent proposed this classification" not in page
    found = api.get("/channels/funny-memes/episodes/1/classification").json()
    assert (found["is_reviewed"], found["classification"]["method"]) == (True, "ai")


def test_classification_confirm_stale_is_conflict(owner: TestClient) -> None:
    submit(owner, TAB, {"group": "tone", "tones": "warm", "revision": "0"})
    assert submit(owner, TAB + "/confirm", {"revision": "0"}).status_code == 409


def test_classification_tab_for_viewer_shows_facts_without_form(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    submit(owner, TAB, {"group": "tone", "tones": "warm", "revision": "0"})
    viewer = join(app, mailer, owner, "vi@example.com", "viewer")
    page = viewer.get(TAB).text
    assert "Warm" in page and 'id="edit-tone"' not in page
    assert submit(viewer, TAB, {"group": "tone", "tones": "serious"}).status_code == 403


def test_profile_shows_declared_and_drift(owner: TestClient) -> None:
    flash_after(
        owner, submit(owner, PROFILE, {"group": "tone", "tones": "serious", "revision": "0"})
    )
    submit(owner, TAB, {"group": "tone", "tones": "ironic", "revision": "0"})
    page = owner.get(PROFILE).text
    assert "Declared: Serious" in page and "Episodes: Ironic 100%" in page
    assert "The episodes drift from the profile" in page


def test_profile_without_episodes_says_nothing_to_compare(owner: TestClient) -> None:
    assert "nothing to compare" in owner.get(PROFILE).text


def test_profile_invalid_reopens_dialog_with_error(owner: TestClient) -> None:
    response = submit(owner, PROFILE, {"group": "genre", "approaches": "parodic", "revision": "0"})
    assert response.status_code == 422
    assert 'id="parody_references-error"' in response.text


def test_profile_for_contributor_hides_edit_and_forbids_post(
    app: FastAPI, mailer: RecordingMailer, owner: TestClient
) -> None:
    contributor = join(app, mailer, owner, "co@example.com", "contributor")
    assert 'id="edit-tone"' not in contributor.get(PROFILE).text
    assert submit(contributor, PROFILE, {"group": "tone", "tones": "serious"}).status_code == 403
