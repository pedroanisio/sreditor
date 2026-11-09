"""Third-party media: credits and warnings, the store, the API and the MCP tools."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.assets import AssetStore, UnsupportedFile
from sreditor.database import Database
from sreditor.episode_texts import PartChanged
from sreditor.episodes import EpisodeInput, EpisodeStore
from sreditor.external_media import (
    ExternalKind,
    ExternalMediaInput,
    ExternalMediaInUse,
    ExternalMediaNotFound,
    ExternalMediaStore,
    MediaUseInput,
    NewCopy,
    Problem,
    UseRefused,
    check_fits,
    copy_accept,
    copy_formats,
    episode_credits,
)
from sreditor.external_media_tools import external_media_error_message
from sreditor.settings import AppSettings
from tests import media_samples as sample
from tests.conftest import RecordingMailer
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, operation_specs, tool
from tests.test_api import FUNNY_MEMES
from tests.test_episodes import joined

LIBRARY = "/channels/funny-memes/external-media"
USES = "/channels/funny-memes/episodes/1/external-media"
CREDITS = "/channels/funny-memes/episodes/1/credits"
NASA = {
    "kind": "video",
    "title": "Mars landing animation",
    "url": "https://svs.gsfc.nasa.gov/mars",
    "creator": "NASA",
    "rights": "public_domain",
}
PAPER = {
    "kind": "document",
    "title": "Mondays and mood",
    "url": "https://example.com/paper",
    "creator": "Silva et al.",
    "rights": "citation",
}


def item_input(**fields: Any) -> ExternalMediaInput:
    return ExternalMediaInput.model_validate({**NASA, **fields})


@pytest.fixture
def media_store(tmp_path: Path) -> ExternalMediaStore:
    """A store over a database with episode 1 of funny-memes and a small upload limit."""
    database = Database(tmp_path / "app.sqlite")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO channels (slug, document, created_at, updated_at)"
            " VALUES ('funny-memes', '{}', '2026-01-01', '2026-01-01')"
        )
    EpisodeStore(database).create("funny-memes", EpisodeInput(title="Pilot"))
    return ExternalMediaStore(database, AssetStore(tmp_path / "assets", 1024), 1024)


# Pure rules


def test_copy_formats_of_web_page_names_pdf_and_screenshots() -> None:
    assert "PDF" in copy_formats(ExternalKind.WEB_PAGE)
    assert copy_accept(ExternalKind.DOCUMENT) == "application/pdf,.pdf"


def test_use_input_end_before_start_is_refused() -> None:
    with pytest.raises(ValueError, match="end"):
        MediaUseInput(media_id=uuid4(), start_seconds=10, end_seconds=5)


def test_check_fits_time_range_on_image_is_refused(media_store: ExternalMediaStore) -> None:
    image = media_store.create("funny-memes", item_input(kind="image"))
    with pytest.raises(UseRefused) as refused:
        check_fits(image, MediaUseInput(media_id=image.id, start_seconds=1))
    assert refused.value.field == "start_seconds"


def test_check_fits_pages_on_video_is_refused(media_store: ExternalMediaStore) -> None:
    video = media_store.create("funny-memes", item_input())
    with pytest.raises(UseRefused) as refused:
        check_fits(video, MediaUseInput(media_id=video.id, pages="3-4"))
    assert refused.value.field == "pages"


def test_episode_credits_lists_each_item_once_in_first_use_order(
    media_store: ExternalMediaStore,
) -> None:
    nasa = media_store.create("funny-memes", item_input(attribution="Credit: NASA/JPL"))
    paper = media_store.create(
        "funny-memes", item_input(kind="document", title="Paper", creator="Ana")
    )
    for media_id in (paper.id, nasa.id, paper.id):
        media_store.add_use("funny-memes", 1, MediaUseInput(media_id=media_id))
    found = episode_credits(
        media_store.all_uses("funny-memes", 1), {nasa.id: nasa, paper.id: paper}
    )
    assert [line.text for line in found.credits] == ["Paper — Ana", "Credit: NASA/JPL"]


def test_episode_credits_use_credit_wins_over_item_attribution(
    media_store: ExternalMediaStore,
) -> None:
    nasa = media_store.create("funny-memes", item_input(attribution="NASA"))
    media_store.add_use(
        "funny-memes", 1, MediaUseInput(media_id=nasa.id, credit="NASA/JPL-Caltech")
    )
    found = episode_credits(media_store.all_uses("funny-memes", 1), {nasa.id: nasa})
    assert found.credits[0].text == "NASA/JPL-Caltech"


def problems_for(
    media_store: ExternalMediaStore, use: MediaUseInput, **fields: Any
) -> list[Problem]:
    item = media_store.create("funny-memes", item_input(**fields))
    media_store.add_use("funny-memes", 1, use.model_copy(update={"media_id": item.id}))
    found = episode_credits(media_store.all_uses("funny-memes", 1), {item.id: item})
    return [warning.problem for warning in found.warnings]


def test_episode_credits_unknown_rights_warns(media_store: ExternalMediaStore) -> None:
    assert problems_for(media_store, MediaUseInput(media_id=uuid4()), rights="unknown") == [
        Problem.RIGHTS_UNKNOWN
    ]


def test_episode_credits_citation_without_reason_range_or_credit_warns_each(
    media_store: ExternalMediaStore,
) -> None:
    found = problems_for(media_store, MediaUseInput(media_id=uuid4()), rights="citation")
    assert found == [Problem.NO_REASON, Problem.NO_RANGE, Problem.NO_CREDIT]


def test_episode_credits_complete_citation_has_no_warning(media_store: ExternalMediaStore) -> None:
    use = MediaUseInput(media_id=uuid4(), start_seconds=3, end_seconds=9)
    fields = {"rights": "citation", "rights_note": "Criticism", "attribution": "Globo"}
    assert problems_for(media_store, use, **fields) == []


# Store


def test_external_media_store_create_without_access_date_uses_today(
    media_store: ExternalMediaStore,
) -> None:
    item = media_store.create("funny-memes", item_input())
    assert item.revision == 1 and isinstance(item.accessed_on, date)
    assert item.copy_file is None


def test_external_media_store_list_filters_by_kind(media_store: ExternalMediaStore) -> None:
    media_store.create("funny-memes", item_input())
    media_store.create("funny-memes", item_input(kind="image", title="Photo"))
    found = media_store.list("funny-memes", None, 10, ExternalKind.IMAGE)
    assert [item.title for item in found] == ["Photo"]


def test_external_media_store_get_of_other_channel_raises_not_found(
    media_store: ExternalMediaStore,
) -> None:
    item = media_store.create("funny-memes", item_input())
    with pytest.raises(ExternalMediaNotFound):
        media_store.get("other", item.id)


def test_external_media_store_replace_stale_revision_raises_changed(
    media_store: ExternalMediaStore,
) -> None:
    item = media_store.create("funny-memes", item_input())
    media_store.replace(item, item_input(title="New"), 1)
    with pytest.raises(PartChanged):
        media_store.replace(item, item_input(title="Mine"), 1)


def test_external_media_store_attach_copy_keeps_type_from_content(
    media_store: ExternalMediaStore,
) -> None:
    item = media_store.create("funny-memes", item_input(kind="document"))
    attached = media_store.attach(NewCopy(item=item, expected_revision=1), io.BytesIO(sample.PDF))
    assert attached.copy_file is not None and attached.copy_file.media_type == "application/pdf"
    assert not attached.copy_file.is_inline() and attached.revision == 2


def test_external_media_store_attach_wrong_type_is_rejected(
    media_store: ExternalMediaStore,
) -> None:
    item = media_store.create("funny-memes", item_input(kind="image"))
    with pytest.raises(UnsupportedFile):
        media_store.attach(NewCopy(item=item), io.BytesIO(sample.PDF))


def test_external_media_store_detach_clears_copy(media_store: ExternalMediaStore) -> None:
    item = media_store.create("funny-memes", item_input(kind="image"))
    attached = media_store.attach(NewCopy(item=item), io.BytesIO(sample.PNG))
    assert media_store.detach(attached, None).copy_file is None


def test_external_media_store_delete_in_use_raises_with_episodes(
    media_store: ExternalMediaStore,
) -> None:
    item = media_store.create("funny-memes", item_input())
    use = media_store.add_use("funny-memes", 1, MediaUseInput(media_id=item.id))
    with pytest.raises(ExternalMediaInUse) as raised:
        media_store.delete(item, None)
    assert raised.value.episodes == [1]
    media_store.delete_use(use, 1)
    media_store.delete(item, 1)
    assert media_store.list("funny-memes", None, 10) == []


def test_external_media_store_delete_stale_revision_raises_changed(
    media_store: ExternalMediaStore,
) -> None:
    item = media_store.create("funny-memes", item_input())
    with pytest.raises(PartChanged):
        media_store.delete(item, 7)


def test_external_media_store_replace_use_stale_revision_raises_changed(
    media_store: ExternalMediaStore,
) -> None:
    item = media_store.create("funny-memes", item_input())
    use = media_store.add_use("funny-memes", 1, MediaUseInput(media_id=item.id))
    media_store.replace_use(use, MediaUseInput(media_id=item.id, scene="Intro"), 1)
    with pytest.raises(PartChanged):
        media_store.replace_use(use, MediaUseInput(media_id=item.id), 1)


# API


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def nasa(owner: TestClient) -> dict[str, Any]:
    response = owner.post(LIBRARY, json=NASA)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def item_path(item: dict[str, Any]) -> str:
    return f"{LIBRARY}/{item['id']}"


def upload_copy(client: TestClient, item: dict[str, Any], content: bytes) -> Any:
    return client.put(f"{item_path(item)}/copy", files={"file": ("copy.bin", content)})


def test_external_media_create_returns_item_without_copy(nasa: dict[str, Any]) -> None:
    assert nasa["rights"] == "public_domain" and nasa["copy_file"] is None


def test_external_media_create_with_idempotency_key_replays(owner: TestClient) -> None:
    headers = {"Idempotency-Key": "nasa-1"}
    first = owner.post(LIBRARY, json=NASA, headers=headers)
    again = owner.post(LIBRARY, json=NASA, headers=headers)
    assert again.json()["id"] == first.json()["id"]


def test_external_media_create_with_bad_link_returns_unprocessable(owner: TestClient) -> None:
    assert owner.post(LIBRARY, json={**NASA, "url": "mars"}).status_code == 422


def test_external_media_list_paginates_and_filters(owner: TestClient, nasa: dict[str, Any]) -> None:
    owner.post(LIBRARY, json=PAPER)
    first = owner.get(LIBRARY, params={"limit": 1}).json()
    rest = owner.get(LIBRARY, params={"cursor": first["next_cursor"]}).json()
    assert [item["title"] for item in first["items"] + rest["items"]] == [
        NASA["title"],
        PAPER["title"],
    ]
    documents = owner.get(LIBRARY, params={"kind": "document"}).json()["items"]
    assert [item["title"] for item in documents] == [PAPER["title"]]


def test_external_media_get_unknown_returns_not_found_naming_the_fix(owner: TestClient) -> None:
    response = owner.get(f"{LIBRARY}/{uuid4()}")
    assert response.status_code == 404 and "list_external_media" in response.json()["detail"]


def test_external_media_put_stale_etag_returns_precondition_failed(
    owner: TestClient, nasa: dict[str, Any]
) -> None:
    changed = owner.put(item_path(nasa), json={**NASA, "title": "New"}, headers={"If-Match": '"1"'})
    assert changed.status_code == 200 and changed.headers["ETag"] == '"2"'
    stale = owner.put(item_path(nasa), json=NASA, headers={"If-Match": '"1"'})
    assert stale.status_code == 412


def test_external_media_image_copy_plays_inline(owner: TestClient) -> None:
    photo = owner.post(LIBRARY, json={**NASA, "kind": "image"}).json()
    assert upload_copy(owner, photo, sample.PNG).status_code == 200
    content = owner.get(f"{item_path(photo)}/copy/content")
    assert content.status_code == 200 and content.content == sample.PNG
    assert content.headers["content-disposition"].startswith("inline")
    assert content.headers["x-content-type-options"] == "nosniff"


def test_external_media_pdf_copy_downloads_as_attachment(owner: TestClient) -> None:
    paper = owner.post(LIBRARY, json=PAPER).json()
    upload_copy(owner, paper, sample.PDF)
    content = owner.get(f"{item_path(paper)}/copy/content")
    assert content.headers["content-disposition"].startswith("attachment")
    assert content.headers["content-type"] == "application/pdf"


def test_external_media_copy_of_wrong_type_returns_unprocessable(
    owner: TestClient, nasa: dict[str, Any]
) -> None:
    response = upload_copy(owner, nasa, sample.PDF)
    assert response.status_code == 422 and response.json()["errors"][0]["location"][-1] == "file"


def test_external_media_copy_content_without_copy_returns_not_found(
    owner: TestClient, nasa: dict[str, Any]
) -> None:
    assert owner.get(f"{item_path(nasa)}/copy/content").status_code == 404


def test_external_media_copy_removed_answers_not_found(owner: TestClient) -> None:
    paper = owner.post(LIBRARY, json=PAPER).json()
    upload_copy(owner, paper, sample.PDF)
    assert owner.delete(f"{item_path(paper)}/copy").json()["copy_file"] is None
    assert owner.get(f"{item_path(paper)}/copy/content").status_code == 404


def test_external_media_copy_for_non_member_returns_not_found(
    owner: TestClient, sign_up: Callable[..., TestClient]
) -> None:
    paper = owner.post(LIBRARY, json=PAPER).json()
    upload_copy(owner, paper, sample.PDF)
    stranger = sign_up("stranger@example.com")
    assert stranger.get(f"{item_path(paper)}/copy/content").status_code == 404


def test_external_media_use_add_list_and_credits(owner: TestClient, nasa: dict[str, Any]) -> None:
    use = {"media_id": nasa["id"], "start_seconds": 5, "end_seconds": 12, "purpose": "evidence"}
    created = owner.post(USES, json=use)
    assert created.status_code == 201, created.text
    assert owner.get(USES).json()["items"][0]["id"] == created.json()["id"]
    credits = owner.get(CREDITS).json()
    assert credits["credits"][0]["text"] == "Mars landing animation — NASA"
    assert credits["warnings"] == []


def test_external_media_use_range_on_document_returns_unprocessable_naming_field(
    owner: TestClient,
) -> None:
    paper = owner.post(LIBRARY, json=PAPER).json()
    response = owner.post(USES, json={"media_id": paper["id"], "start_seconds": 1})
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["body", "start_seconds"]


def test_external_media_use_of_other_channel_item_returns_not_found(
    owner: TestClient,
) -> None:
    assert owner.post(USES, json={"media_id": str(uuid4())}).status_code == 404


def test_external_media_use_put_and_delete_with_etags(
    owner: TestClient, nasa: dict[str, Any]
) -> None:
    use = owner.post(USES, json={"media_id": nasa["id"]}).json()
    path = f"{USES}/{use['id']}"
    assert owner.get(path).headers["ETag"] == '"1"'
    changed = owner.put(
        path, json={"media_id": nasa["id"], "scene": "Intro"}, headers={"If-Match": '"1"'}
    )
    assert changed.json()["scene"] == "Intro"
    assert owner.delete(path, headers={"If-Match": '"1"'}).status_code == 412
    assert owner.delete(path, headers={"If-Match": '"2"'}).status_code == 204
    assert owner.get(path).status_code == 404


def test_external_media_delete_in_use_returns_conflict_naming_episodes(
    owner: TestClient, nasa: dict[str, Any]
) -> None:
    owner.post(USES, json={"media_id": nasa["id"]})
    response = owner.delete(item_path(nasa))
    assert response.status_code == 409 and response.json()["episodes"] == [1]
    assert owner.get(f"{item_path(nasa)}/episodes").json() == [1]


def test_external_media_delete_unused_returns_no_content(
    owner: TestClient, nasa: dict[str, Any]
) -> None:
    assert owner.delete(item_path(nasa)).status_code == 204


def test_external_media_create_as_viewer_returns_forbidden(
    owner: TestClient, sign_up: Callable[..., TestClient], mailer: RecordingMailer
) -> None:
    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.post(LIBRARY, json=NASA).status_code == 403


def test_external_media_delete_as_contributor_returns_forbidden(
    owner: TestClient,
    sign_up: Callable[..., TestClient],
    mailer: RecordingMailer,
    nasa: dict[str, Any],
) -> None:
    contributor = joined(owner, sign_up, mailer, "contributor")
    assert contributor.post(USES, json={"media_id": nasa["id"]}).status_code == 201
    assert contributor.delete(item_path(nasa)).status_code == 403


def test_external_media_create_with_token_blocked_by_gate_is_forbidden(
    app: FastAPI, app_settings: AppSettings, owner: TestClient
) -> None:
    block(app_settings, "/", "external_media.create")
    response = agent(app, new_token(owner)).post(LIBRARY, json=NASA)
    assert response.status_code == 403


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def added(mcp: TestClient, item: dict[str, Any]) -> dict[str, Any]:
    result = tool(mcp, "add_external_media", {"slug": "funny-memes", "item": item})
    assert not result.get("isError"), result
    found: dict[str, Any] = result["structuredContent"]
    return found


def test_mcp_add_external_media_then_list_by_kind_returns_it(mcp: TestClient) -> None:
    added(mcp, PAPER)
    found = tool(mcp, "list_external_media", {"slug": "funny-memes", "kind": "document"})
    assert [item["title"] for item in found["structuredContent"]["items"]] == [PAPER["title"]]


def test_mcp_update_external_media_records_credit(mcp: TestClient) -> None:
    item = added(mcp, PAPER)
    arguments = {
        "slug": "funny-memes",
        "media_id": item["id"],
        "item": {**PAPER, "attribution": "Silva, 2025"},
        "expected_revision": 1,
    }
    result = tool(mcp, "update_external_media", arguments)
    assert result["structuredContent"]["attribution"] == "Silva, 2025"


def test_mcp_use_external_media_shows_in_credits_with_warnings(mcp: TestClient) -> None:
    item = added(mcp, PAPER)
    use = {"media_id": item["id"], "pages": "3-4", "purpose": "quote"}
    tool(mcp, "use_external_media", {"slug": "funny-memes", "number": 1, "use": use})
    found = tool(mcp, "get_episode_credits", {"slug": "funny-memes", "number": 1})
    content = found["structuredContent"]
    assert content["credits"]["credits"][0]["text"] == "Mondays and mood — Silva et al."
    problems = [warning["problem"] for warning in content["credits"]["warnings"]]
    assert problems == ["no_reason", "no_credit"]
    assert content["uses"]["items"][0]["pages"] == "3-4"


def test_mcp_use_range_on_document_is_a_tool_error_naming_field(mcp: TestClient) -> None:
    item = added(mcp, PAPER)
    use = {"media_id": item["id"], "start_seconds": 1}
    result = tool(mcp, "use_external_media", {"slug": "funny-memes", "number": 1, "use": use})
    assert result["isError"] and "start_seconds" in result["content"][0]["text"]


def test_mcp_remove_external_media_use_drops_it(mcp: TestClient) -> None:
    item = added(mcp, NASA)
    use = tool(
        mcp,
        "use_external_media",
        {"slug": "funny-memes", "number": 1, "use": {"media_id": item["id"]}},
    )["structuredContent"]
    arguments = {"slug": "funny-memes", "number": 1, "use_id": use["id"]}
    removed = tool(mcp, "remove_external_media_use", arguments)
    assert removed["structuredContent"]["kind"] == "external_media_use"
    again = tool(mcp, "remove_external_media_use", arguments)
    assert again["isError"] and "get_episode_credits" in again["content"][0]["text"]


def test_mcp_use_unknown_item_names_list_tool(mcp: TestClient) -> None:
    use = {"media_id": str(uuid4())}
    result = tool(mcp, "use_external_media", {"slug": "funny-memes", "number": 1, "use": use})
    assert result["isError"] and "list_external_media" in result["content"][0]["text"]


def test_external_media_error_message_in_use_names_remove_tool() -> None:
    message = external_media_error_message(ExternalMediaInUse(uuid4(), [1, 2]))
    assert message is not None and "1, 2" in message and "remove_external_media_use" in message


def test_external_media_error_message_other_error_is_none() -> None:
    assert external_media_error_message(ValueError("x")) is None


def used(mcp: TestClient, **fields: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """A paper and its use in episode 1, as the tools return them."""
    item = added(mcp, PAPER)
    use = {"media_id": item["id"], "pages": "3-4", "purpose": "quote", **fields}
    made = tool(mcp, "use_external_media", {"slug": "funny-memes", "number": 1, "use": use})
    return item, made["structuredContent"]


def update_use(mcp: TestClient, made: dict[str, Any], use: dict[str, Any], **extra: Any) -> Any:
    target = {"number": 1, "use_id": made["id"]}
    return tool(
        mcp,
        "update_external_media_use",
        {"slug": "funny-memes", "target": target, "use": use, **extra},
    )


def test_mcp_update_external_media_use_fixes_its_warning(mcp: TestClient) -> None:
    item, made = used(mcp)
    fixed = {"media_id": item["id"], "pages": "3-4", "purpose": "quote", "credit": "Silva, 2025"}
    done = update_use(mcp, made, fixed, expected_revision=made["revision"])["structuredContent"]
    found = tool(mcp, "get_episode_credits", {"slug": "funny-memes", "number": 1})
    problems = [warning["problem"] for warning in found["structuredContent"]["credits"]["warnings"]]
    assert done["id"] == made["id"] and "no_credit" not in problems


def test_mcp_update_external_media_use_with_stale_revision_is_a_tool_error(
    mcp: TestClient,
) -> None:
    item, made = used(mcp)
    use = {"media_id": item["id"], "pages": "5", "purpose": "quote"}
    update_use(mcp, made, use)
    result = update_use(mcp, made, use, expected_revision=made["revision"])
    assert result["isError"] and "revision 2" in result["content"][0]["text"]


def test_mcp_update_external_media_use_unknown_use_points_at_get_episode_credits(
    mcp: TestClient,
) -> None:
    item, _ = used(mcp)
    use = {"media_id": item["id"], "pages": "5", "purpose": "quote"}
    result = update_use(mcp, {"id": str(uuid4())}, use)
    assert result["isError"] and "call get_episode_credits" in result["content"][0]["text"]


def test_mcp_update_external_media_use_unknown_item_points_at_list_external_media(
    mcp: TestClient,
) -> None:
    _, made = used(mcp)
    result = update_use(mcp, made, {"media_id": str(uuid4()), "purpose": "quote"})
    assert result["isError"] and "call list_external_media" in result["content"][0]["text"]


def test_mcp_get_episode_credits_description_points_use_warnings_at_update_use(
    app: FastAPI,
) -> None:
    listed = operation_specs(app)
    assert "update_external_media_use" in listed["get_episode_credits"]["description"]
