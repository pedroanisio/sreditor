"""Masters made outside sreditor: final_video uploads, references and cut rules (project.delivery)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sreditor.delivery import Cut, DeliveryWarningKind
from sreditor.settings import DEFAULT_AI_NOTICE_PATTERN, DeliveryRules, delivery_rules
from sreditor.video_probe import probe_video, read_loudness
from tests import media_samples as sample
from tests.test_agentic import PUBLIC_URL, agent, new_token, tool
from tests.test_delivery import DELIVERY, MEDIA, owner, package, upload

HAS_FFMPEG = shutil.which("ffmpeg") is not None
SHA = "a" * 64
MASTER = {"location": "Drive/Arc4d3/ep1/final.mp4", "sha256": SHA, "duration_seconds": 20.5}

__all__ = ["owner"]


@pytest.fixture(scope="module")
def master_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real one-second 64x48 MP4 with a tone, made with ffmpeg."""
    if not HAS_FFMPEG:
        pytest.skip("needs ffmpeg to make a video")
    path = tmp_path_factory.mktemp("master") / "master.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=10:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


def cut_with(**fields: Any) -> dict[str, Any]:
    """A package whose one cut has these fields, over the usual caption with an AI notice."""
    body = package()
    body["cuts"][0].update(fields)
    return body


def warning_kinds(owner: TestClient) -> list[str]:
    return [item["kind"] for item in owner.get(DELIVERY).json()["warnings"]]


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


# Measuring


def test_probe_video_reads_duration_size_and_loudness(master_file: Path) -> None:
    found = probe_video(master_file)
    assert found is not None
    assert (found.duration_seconds, found.width, found.height) == (1.0, 64, 48)
    assert found.loudness_lufs is not None and found.loudness_lufs < 0


def test_probe_video_of_a_file_it_cannot_read_is_none(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(sample.MP4)
    assert probe_video(broken) is None


def test_read_loudness_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert read_loudness(tmp_path / "absent.mp4") is None


# Uploading a master


def test_final_video_upload_keeps_the_measures(owner: TestClient, master_file: Path) -> None:
    response = owner.post(
        MEDIA,
        data={"kind": "final_video"},
        files={"file": ("master.mp4", master_file.read_bytes())},
    )
    assert response.status_code == 201, response.text
    measures = response.json()["measures"]
    assert (measures["duration_seconds"], measures["width"], measures["height"]) == (1.0, 64, 48)
    assert measures["loudness_lufs"] is not None


def test_final_video_list_shows_the_measures(owner: TestClient, master_file: Path) -> None:
    owner.post(
        MEDIA,
        data={"kind": "final_video"},
        files={"file": ("master.mp4", master_file.read_bytes())},
    )
    listed = owner.get(MEDIA, params={"kind": "final_video"}).json()["items"]
    assert listed[0]["measures"]["duration_seconds"] == 1.0


def test_final_video_unreadable_by_ffprobe_is_kept_without_measures(owner: TestClient) -> None:
    media_id = upload(owner, "final_video", sample.MP4, "master.mp4")
    assert owner.get(f"{MEDIA}/{media_id}").json()["measures"] is None


def test_final_video_that_is_not_a_video_is_refused(owner: TestClient) -> None:
    response = owner.post(
        MEDIA, data={"kind": "final_video"}, files={"file": ("a.png", sample.PNG)}
    )
    assert response.status_code == 422


def test_final_video_is_accepted_as_a_cuts_video(owner: TestClient) -> None:
    media_id = upload(owner, "final_video", sample.MP4, "master.mp4")
    assert owner.put(DELIVERY, json=cut_with(video_id=media_id)).status_code == 200


def test_upload_link_for_final_video_accepts_video_types(mcp: TestClient) -> None:
    upload_request = {"number": 1, "kind": "final_video", "name": "Master"}
    result = tool(mcp, "create_upload_link", {"slug": "funny-memes", "upload": upload_request})
    assert result["structuredContent"]["accepted_types"] == ["video/mp4", "video/webm"]


def test_upload_link_for_cover_accepts_image_types(mcp: TestClient) -> None:
    upload_request = {"number": 1, "kind": "cover", "name": "Cover"}
    result = tool(mcp, "create_upload_link", {"slug": "funny-memes", "upload": upload_request})
    assert result["structuredContent"]["accepted_types"] == [
        "image/png",
        "image/jpeg",
        "image/webp",
    ]


# A master by reference


def test_cut_with_a_referenced_master_has_no_missing_video_warning(owner: TestClient) -> None:
    assert owner.put(DELIVERY, json=cut_with(external_video=MASTER)).status_code == 200
    assert warning_kinds(owner) == []


def test_cut_reference_is_stored_as_written(owner: TestClient) -> None:
    owner.put(DELIVERY, json=cut_with(external_video=MASTER))
    stored = owner.get(DELIVERY).json()["cuts"][0]["external_video"]
    assert (stored["location"], stored["sha256"]) == (MASTER["location"], SHA)


def test_cut_with_uploaded_and_referenced_video_is_refused() -> None:
    with pytest.raises(ValidationError, match="not both"):
        Cut(name="main", video_id="7c1c3c4a-0d41-4a37-9a3e-1b2c3d4e5f60", external_video=MASTER)  # type: ignore[arg-type]


def test_cut_reference_with_a_bad_hash_is_refused(owner: TestClient) -> None:
    bad = {**MASTER, "sha256": "xyz"}
    assert owner.put(DELIVERY, json=cut_with(external_video=bad)).status_code == 422


# The cut's rules


def test_cut_of_a_referenced_master_over_the_length_warns(owner: TestClient) -> None:
    owner.put(DELIVERY, json=cut_with(external_video={**MASTER, "duration_seconds": 30}))
    assert warning_kinds(owner) == [DeliveryWarningKind.TOO_LONG.value]


def test_cut_of_a_referenced_master_at_the_limit_does_not_warn(owner: TestClient) -> None:
    owner.put(DELIVERY, json=cut_with(external_video={**MASTER, "duration_seconds": 24}))
    assert warning_kinds(owner) == []


def test_cut_with_more_than_five_hashtags_warns(owner: TestClient) -> None:
    tags = ["a", "b", "c", "d", "e", "f"]
    owner.put(DELIVERY, json=cut_with(external_video=MASTER, hashtags=tags))
    assert warning_kinds(owner) == [DeliveryWarningKind.TOO_MANY_HASHTAGS.value]


def test_cut_with_five_hashtags_does_not_warn(owner: TestClient) -> None:
    owner.put(DELIVERY, json=cut_with(external_video=MASTER, hashtags=list("abcde")))
    assert warning_kinds(owner) == []


def test_cut_caption_without_the_ai_notice_warns(owner: TestClient) -> None:
    owner.put(DELIVERY, json=cut_with(external_video=MASTER, caption="Oi, gente!"))
    assert warning_kinds(owner) == [DeliveryWarningKind.NO_AI_NOTICE.value]


@pytest.mark.parametrize(
    "caption", ["Feito com IA", "Gerado por AI.", "Usa inteligência artificial", "AI generated"]
)
def test_cut_caption_with_an_ai_notice_does_not_warn(owner: TestClient, caption: str) -> None:
    owner.put(DELIVERY, json=cut_with(external_video=MASTER, caption=caption))
    assert warning_kinds(owner) == []


def test_cut_caption_with_a_word_that_only_contains_ia_still_warns(owner: TestClient) -> None:
    owner.put(DELIVERY, json=cut_with(external_video=MASTER, caption="Que alegria!"))
    assert warning_kinds(owner) == [DeliveryWarningKind.NO_AI_NOTICE.value]


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg to make a video")
def test_cut_of_an_uploaded_master_over_the_length_warns(
    owner: TestClient, app: FastAPI, master_file: Path
) -> None:
    app.state.services.settings = app.state.services.settings.model_copy(
        update={"delivery": DeliveryRules(max_seconds=0.5)}
    )
    response = owner.post(
        MEDIA, data={"kind": "final_video"}, files={"file": ("m.mp4", master_file.read_bytes())}
    )
    owner.put(DELIVERY, json=cut_with(video_id=response.json()["id"]))
    assert warning_kinds(owner) == [DeliveryWarningKind.TOO_LONG.value]


def test_published_episode_warns_about_a_cut_over_the_length(owner: TestClient) -> None:
    owner.put(DELIVERY, json=cut_with(external_video={**MASTER, "duration_seconds": 40}))
    body = {"title": "Pilot", "status": "published"}
    response = owner.put("/channels/funny-memes/episodes/1", json=body)
    assert [item["kind"] for item in response.json()["warnings"]] == ["too_long"]


# Settings


def test_delivery_rules_default_to_the_channels_limits() -> None:
    rules = delivery_rules({}, {})
    assert (rules.max_seconds, rules.max_hashtags) == (24, 5)
    assert rules.ai_notice_pattern == DEFAULT_AI_NOTICE_PATTERN


def test_delivery_rules_read_the_environment() -> None:
    environ = {
        "SREDITOR_DELIVERY_MAX_SECONDS": "60",
        "SREDITOR_DELIVERY_MAX_HASHTAGS": "10",
        "SREDITOR_DELIVERY_AI_NOTICE_PATTERN": "",
    }
    rules = delivery_rules(environ, {})
    assert (rules.max_seconds, rules.max_hashtags, rules.ai_notice_pattern) == (60, 10, "")


def test_delivery_rules_read_pyproject_when_the_environment_is_silent() -> None:
    rules = delivery_rules({}, {"delivery_max_seconds": "30", "delivery_max_hashtags": "8"})
    assert (rules.max_seconds, rules.max_hashtags) == (30, 8)


def test_delivery_rules_refuse_a_broken_pattern() -> None:
    with pytest.raises(ValidationError, match="regular expression"):
        DeliveryRules(ai_notice_pattern="(")


def test_cut_caption_check_is_off_with_an_empty_pattern(owner: TestClient, app: FastAPI) -> None:
    app.state.services.settings = app.state.services.settings.model_copy(
        update={"delivery": DeliveryRules(ai_notice_pattern="")}
    )
    owner.put(DELIVERY, json=cut_with(external_video=MASTER, caption=""))
    assert warning_kinds(owner) == []
