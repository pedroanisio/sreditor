"""Who speaks each short-script block and how the cast moves (rules project.srstudio.beats, .voice)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sreditor.short_script import Beat, Block, BlockFunction
from sreditor.srstudio_vocabulary import Arms, Eyes, Mouth, Wear
from tests.test_api import FUNNY_MEMES

CHANNEL = "/channels/funny-memes"
SHORT_SCRIPT = f"{CHANNEL}/episodes/1/short-script"


def beat(at_s: float, **changes: Any) -> dict[str, Any]:
    """A beat's fields, Rex at rest unless changed."""
    return {
        "at_s": at_s,
        "character": str(uuid4()),
        "eyes": "wide",
        "mouth": "flat",
        "arms": "desk",
        **changes,
    }


def block_with(beats: list[dict[str, Any]], end_s: float = 2) -> Block:
    """A hook block holding these beats."""
    return Block(start_s=0, end_s=end_s, function=BlockFunction.HOOK, speech="Oi.", beats=beats)


def test_block_with_beats_keeps_their_poses() -> None:
    kept = block_with([beat(0), beat(0.15, eyes="shock", wear=["glasses"])])
    assert kept.beats[1].eyes is Eyes.SHOCK and kept.beats[1].wear == [Wear.GLASSES]


def test_block_first_beat_after_zero_is_refused() -> None:
    with pytest.raises(ValidationError, match="starts at 0 s"):
        block_with([beat(0.5)])


def test_block_beats_closer_than_step_are_refused() -> None:
    with pytest.raises(ValidationError, match="0.15 s apart"):
        block_with([beat(0), beat(0.1)])


def test_block_beats_at_float_sum_of_step_are_accepted() -> None:
    assert len(block_with([beat(0), beat(0.1 + 0.05)]).beats) == 2


def test_block_beat_at_its_end_is_refused() -> None:
    with pytest.raises(ValidationError, match="before its block ends"):
        block_with([beat(0), beat(2)])


def test_beat_outside_toon_vocabulary_is_refused() -> None:
    with pytest.raises(ValidationError):
        Beat.model_validate(beat(0, eyes="sparkly"))


def test_beat_wearing_item_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="at most once"):
        Beat.model_validate(beat(0, wear=["sweat", "sweat"]))


def test_beat_action_without_prop_is_refused() -> None:
    with pytest.raises(ValidationError, match="needs a prop"):
        Beat.model_validate(beat(0, action="open"))


def test_beat_defaults_to_no_prop_and_nothing_worn() -> None:
    shown = Beat(at_s=0, character=uuid4(), eyes=Eyes.UP, mouth=Mouth.O, arms=Arms.SIGH)
    assert shown.prop is None and shown.wear == []


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post(f"{CHANNEL}/episodes", json={"title": "Pilot"}).status_code == 201
    return client


def added(owner: TestClient, plural: str, name: str) -> str:
    """Add a library entry and return its id."""
    response = owner.post(f"{CHANNEL}/{plural}", json={"name": name})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def script(**block: Any) -> dict[str, Any]:
    """A one-block script with these block fields."""
    return {
        "duration_s": 2,
        "blocks": [{"start_s": 0, "end_s": 2, "function": "hook", "speech": "Oi.", **block}],
    }


def test_short_script_speaker_from_library_is_saved(owner: TestClient) -> None:
    rex = added(owner, "characters", "Rex")
    response = owner.put(SHORT_SCRIPT, json=script(speaker=rex))
    assert response.status_code == 200 and response.json()["blocks"][0]["speaker"] == rex


def test_short_script_unknown_speaker_names_field_and_tool(owner: TestClient) -> None:
    response = owner.put(SHORT_SCRIPT, json=script(speaker=str(uuid4())))
    problem = response.json()
    assert response.status_code == 422 and "list_library" in problem["detail"]
    assert problem["errors"][0]["location"] == ["body", "blocks", 0, "speaker"]


def test_short_script_place_from_library_is_saved(owner: TestClient) -> None:
    salon = added(owner, "locations", "Salon")
    response = owner.put(SHORT_SCRIPT, json=script(place=salon))
    assert response.status_code == 200 and response.json()["blocks"][0]["place"] == salon


def test_short_script_unknown_place_names_field_and_tool(owner: TestClient) -> None:
    response = owner.put(SHORT_SCRIPT, json=script(place=str(uuid4())))
    problem = response.json()
    assert response.status_code == 422 and "kind location" in problem["detail"]
    assert problem["errors"][0]["location"] == ["body", "blocks", 0, "place"]


def test_short_script_character_as_place_is_refused(owner: TestClient) -> None:
    rex = added(owner, "characters", "Rex")
    assert owner.put(SHORT_SCRIPT, json=script(place=rex)).status_code == 422


def test_short_script_location_as_speaker_is_refused(owner: TestClient) -> None:
    kitchen = added(owner, "locations", "Kitchen")
    assert owner.put(SHORT_SCRIPT, json=script(speaker=kitchen)).status_code == 422


def test_short_script_beat_with_library_prop_is_saved(owner: TestClient) -> None:
    rex = added(owner, "characters", "Rex")
    added(owner, "props", "Laptop")
    beats = [beat(0, character=rex, prop="laptop", action="open")]
    response = owner.put(SHORT_SCRIPT, json=script(beats=beats))
    assert (
        response.status_code == 200 and response.json()["blocks"][0]["beats"][0]["prop"] == "laptop"
    )


def test_short_script_beat_with_unknown_prop_names_it(owner: TestClient) -> None:
    rex = added(owner, "characters", "Rex")
    response = owner.put(SHORT_SCRIPT, json=script(beats=[beat(0, character=rex, prop="hat")]))
    location = response.json()["errors"][0]["location"]
    assert response.status_code == 422 and location == ["body", "blocks", 0, "beats", 0, "prop"]


def test_short_script_beat_with_unknown_character_is_refused(owner: TestClient) -> None:
    response = owner.put(SHORT_SCRIPT, json=script(beats=[beat(0)]))
    location = response.json()["errors"][0]["location"]
    assert response.status_code == 422 and location[-1] == "character"
