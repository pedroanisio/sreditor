"""An episode's delivery package: models, API, published warnings and MCP (project.delivery)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sreditor.delivery import (
    MAX_POST_HASHTAGS,
    Cut,
    DeliveryInput,
    DeliveryStore,
    DeliveryWarningKind,
    Post,
    delivery_key,
)
from tests import media_samples as sample
from tests.test_agentic import PUBLIC_URL, agent, block, new_token, tool
from tests.test_api import FUNNY_MEMES

EPISODE = "/channels/funny-memes/episodes/1"
DELIVERY = f"{EPISODE}/delivery"
MEDIA = f"{EPISODE}/media"
POST_ON_TIKTOK = f"{DELIVERY}/posts/tiktok"


@pytest.fixture
def owner(sign_up: Any) -> TestClient:
    client: TestClient = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post("/channels/funny-memes/episodes", json={"title": "Pilot"}).status_code == 201
    return client


def upload(client: TestClient, kind: str, content: bytes, name: str) -> str:
    response = client.post(MEDIA, data={"kind": kind}, files={"file": (name, content)})
    assert response.status_code == 201, response.text
    media_id: str = response.json()["id"]
    return media_id


@pytest.fixture
def video(owner: TestClient) -> str:
    return upload(owner, "visual_effect", sample.MP4, "final.mp4")


@pytest.fixture
def cover(owner: TestClient) -> str:
    return upload(owner, "cover", sample.PNG, "cover.png")


def package(video_id: str | None = None, cover_id: str | None = None) -> dict[str, Any]:
    return {
        "cuts": [
            {
                "name": "main",
                "video_id": video_id,
                "cover_id": cover_id,
                "caption": "Oi, gente! Feito com IA.",
                "alt_text": "A dog waving.",
                "hashtags": ["#Dogs", "funny"],
            }
        ],
        "posts": [],
    }


# Models


def test_cut_hashtags_with_sigil_are_stored_without_it() -> None:
    cut = Cut(name="main", hashtags=["#dogs", " #funny "])
    assert cut.hashtags == ["dogs", "funny"]


def test_cut_hashtags_with_accents_are_valid() -> None:
    assert Cut(name="main", hashtags=["açaí"]).hashtags == ["açaí"]


def test_cut_hashtags_repeated_ignoring_case_are_refused() -> None:
    with pytest.raises(ValidationError, match="Each hashtag may appear once"):
        Cut(name="main", hashtags=["Dogs", "dogs"])


@pytest.mark.parametrize("tag", ["two words", "a-b", "!"])
def test_cut_hashtag_with_other_characters_is_refused(tag: str) -> None:
    with pytest.raises(ValidationError):
        Cut(name="main", hashtags=[tag])


@pytest.mark.parametrize("name", ["Main", "two words", "-main", ""])
def test_cut_name_that_is_not_a_slug_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        Cut(name=name)


def test_cut_caption_over_limit_is_refused() -> None:
    with pytest.raises(ValidationError):
        Cut(name="main", caption="x" * 1001)


def test_post_url_other_than_http_is_refused() -> None:
    with pytest.raises(ValidationError):
        Post.model_validate({"platform": "tiktok", "cut": "main", "url": "ftp://x.test/1"})


def test_post_published_at_without_zone_is_read_as_utc() -> None:
    post = Post.model_validate(
        {"platform": "tiktok", "cut": "main", "published_at": "2026-11-07T10:30:00"}
    )
    assert post.published_at is not None and post.published_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_post_without_own_text_has_empty_text() -> None:
    post = Post.model_validate({"platform": "tiktok", "cut": "main"})
    assert (post.caption, post.alt_text, post.hashtags) == ("", "", [])


def test_post_hashtags_over_the_post_limit_are_refused() -> None:
    tags = [f"tag{number}" for number in range(MAX_POST_HASHTAGS + 1)]
    with pytest.raises(ValidationError, match="hashtags"):
        Post.model_validate({"platform": "tiktok", "cut": "main", "hashtags": tags})


def test_post_hashtags_repeated_ignoring_case_are_refused() -> None:
    with pytest.raises(ValidationError, match="once"):
        Post.model_validate({"platform": "tiktok", "cut": "main", "hashtags": ["#Dogs", "dogs"]})


def test_delivery_store_reads_package_stored_before_post_text_existed(
    app: FastAPI, owner: TestClient
) -> None:
    old = (
        '{"cuts": [{"name": "main", "caption": "Feito com IA."}],'
        ' "posts": [{"platform": "tiktok", "cut": "main", "url": "https://tiktok.test/v/1"}]}'
    )
    store: DeliveryStore = app.state.services.deliveries
    store.texts.write(delivery_key("funny-memes", 1), old, None)
    (post,) = store.delivery("funny-memes", 1).posts
    assert (post.caption, post.hashtags) == ("", [])


def test_delivery_input_with_post_for_missing_cut_is_refused() -> None:
    with pytest.raises(ValidationError, match="not in this package"):
        DeliveryInput.model_validate({"cuts": [], "posts": [{"platform": "tiktok", "cut": "x"}]})


def test_delivery_input_with_duplicate_cut_names_is_refused() -> None:
    with pytest.raises(ValidationError, match="own name"):
        DeliveryInput.model_validate({"cuts": [{"name": "main"}, {"name": "main"}]})


def test_delivery_input_with_two_posts_on_one_platform_is_refused() -> None:
    post = {"platform": "tiktok", "cut": "main"}
    with pytest.raises(ValidationError, match="at most one post"):
        DeliveryInput.model_validate({"cuts": [{"name": "main"}], "posts": [post, post]})


# API


def test_delivery_get_unwritten_is_empty_with_etag_zero(owner: TestClient) -> None:
    response = owner.get(DELIVERY)
    assert response.headers["etag"] == '"0"' and response.json()["cuts"] == []


def test_delivery_put_returns_normalized_package_and_new_etag(
    owner: TestClient, video: str, cover: str
) -> None:
    response = owner.put(DELIVERY, json=package(video, cover))
    assert response.status_code == 200, response.text
    assert response.headers["etag"] == '"1"'
    assert response.json()["cuts"][0]["hashtags"] == ["Dogs", "funny"]


def test_delivery_put_stale_if_match_returns_precondition_failed(owner: TestClient) -> None:
    owner.put(DELIVERY, json=package())
    response = owner.put(DELIVERY, json=package(), headers={"If-Match": '"0"'})
    assert response.status_code == 412


def test_delivery_put_matching_if_match_succeeds(owner: TestClient) -> None:
    response = owner.put(DELIVERY, json=package(), headers={"If-Match": '"0"'})
    assert response.status_code == 200


def test_delivery_put_video_of_another_kind_is_refused(owner: TestClient) -> None:
    audio = upload(owner, "voice_over", sample.MP3_TAGGED, "take.mp3")
    response = owner.put(DELIVERY, json=package(video_id=audio))
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["cuts", 0, "video_id"]


def test_delivery_put_cover_that_is_a_video_is_refused(owner: TestClient, video: str) -> None:
    response = owner.put(DELIVERY, json=package(cover_id=video))
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["cuts", 0, "cover_id"]


def test_delivery_put_unknown_media_id_is_refused(owner: TestClient) -> None:
    response = owner.put(DELIVERY, json=package(video_id="11111111-1111-1111-1111-111111111111"))
    assert response.status_code == 422


def test_delivery_put_render_video_is_accepted(owner: TestClient, video: str) -> None:
    assert owner.put(DELIVERY, json=package(video_id=video)).status_code == 200


def test_delivery_put_post_on_platform_without_account_is_refused(owner: TestClient) -> None:
    body = {**package(), "posts": [{"platform": "youtube", "cut": "main"}]}
    response = owner.put(DELIVERY, json=body)
    assert response.status_code == 422 and "no youtube account" in response.json()["detail"]


def test_delivery_put_invalid_hashtag_returns_field_errors(owner: TestClient) -> None:
    body = package()
    body["cuts"][0]["hashtags"] = ["two words"]
    assert owner.put(DELIVERY, json=body).status_code == 422


def test_delivery_get_unknown_episode_returns_not_found(owner: TestClient) -> None:
    assert owner.get("/channels/funny-memes/episodes/9/delivery").status_code == 404


def test_record_post_sets_the_platform_post_and_keeps_cuts(owner: TestClient) -> None:
    owner.put(DELIVERY, json=package())
    response = owner.put(
        POST_ON_TIKTOK,
        json={
            "cut": "main",
            "url": "https://tiktok.test/v/1",
            "published_at": "2026-11-07T10:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["cuts"][0]["name"] == "main" and body["posts"][0]["url"] == "https://tiktok.test/v/1"
    )


def test_record_post_replaces_the_earlier_post(owner: TestClient) -> None:
    owner.put(DELIVERY, json=package())
    owner.put(POST_ON_TIKTOK, json={"cut": "main"})
    response = owner.put(POST_ON_TIKTOK, json={"cut": "main", "url": "https://tiktok.test/v/2"})
    assert len(response.json()["posts"]) == 1


def test_record_post_for_unknown_cut_is_refused(owner: TestClient) -> None:
    assert owner.put(POST_ON_TIKTOK, json={"cut": "ghost"}).status_code == 422


def test_record_post_on_unknown_platform_path_is_invalid(owner: TestClient) -> None:
    assert owner.put(f"{DELIVERY}/posts/nope", json={"cut": "main"}).status_code == 422


def test_delivery_get_warns_for_cut_without_video_and_post_without_link(owner: TestClient) -> None:
    owner.put(DELIVERY, json=package())
    owner.put(POST_ON_TIKTOK, json={"cut": "main"})
    kinds = {item["kind"] for item in owner.get(DELIVERY).json()["warnings"]}
    assert kinds == {DeliveryWarningKind.NO_VIDEO.value, DeliveryWarningKind.NO_LINK.value}


def test_delivery_get_complete_package_has_no_warnings(owner: TestClient, video: str) -> None:
    owner.put(DELIVERY, json=package(video_id=video))
    owner.put(POST_ON_TIKTOK, json={"cut": "main", "url": "https://tiktok.test/v/1"})
    assert owner.get(DELIVERY).json()["warnings"] == []


def test_delivery_get_after_video_removed_warns_it_is_gone(owner: TestClient, video: str) -> None:
    owner.put(DELIVERY, json=package(video_id=video))
    assert owner.delete(f"{MEDIA}/{video}").status_code == 204
    kinds = [item["kind"] for item in owner.get(DELIVERY).json()["warnings"]]
    assert kinds == [DeliveryWarningKind.VIDEO_REMOVED.value]


def test_delivery_by_viewer_can_read_but_not_write(
    owner: TestClient, sign_up: Any, mailer: Any
) -> None:
    from tests.test_episodes import joined

    viewer = joined(owner, sign_up, mailer, "viewer")
    assert viewer.get(DELIVERY).status_code == 200
    assert viewer.put(DELIVERY, json=package()).status_code == 403


# Post text


def post_with(**text: Any) -> dict[str, Any]:
    return {"cut": "main", "url": "https://tiktok.test/v/1", **text}


def test_record_post_with_own_text_stores_it_and_get_returns_it(owner: TestClient) -> None:
    owner.put(DELIVERY, json=package())
    text = {"caption": "Para o TikTok. Feito com IA.", "alt_text": "Um cão.", "hashtags": ["#cao"]}
    owner.put(POST_ON_TIKTOK, json=post_with(**text))
    (post,) = owner.get(DELIVERY).json()["posts"]
    assert (post["caption"], post["alt_text"], post["hashtags"]) == (
        "Para o TikTok. Feito com IA.",
        "Um cão.",
        ["cao"],
    )


def test_delivery_put_post_caption_without_ai_notice_is_refused_naming_the_field(
    owner: TestClient,
) -> None:
    body = package()
    body["posts"] = [{"platform": "tiktok", "cut": "main", "caption": "Just a caption."}]
    response = owner.put(DELIVERY, json=body)
    assert response.status_code == 422
    error = response.json()["errors"][0]
    assert error["location"] == ["posts", 0, "caption"] and "AI notice" in error["message"]


def test_delivery_put_post_with_too_many_hashtags_is_refused_naming_the_field(
    owner: TestClient,
) -> None:
    body = package()
    tags = [f"tag{number}" for number in range(MAX_POST_HASHTAGS + 1)]
    body["posts"] = [{"platform": "tiktok", "cut": "main", "hashtags": tags}]
    response = owner.put(DELIVERY, json=body)
    assert response.status_code == 422 and "hashtags" in str(response.json()["errors"])


def test_record_post_caption_without_ai_notice_is_refused_naming_the_field(
    owner: TestClient,
) -> None:
    owner.put(DELIVERY, json=package())
    response = owner.put(POST_ON_TIKTOK, json=post_with(caption="Just a caption."))
    assert response.status_code == 422
    assert response.json()["errors"][0]["location"] == ["caption"]


def test_delivery_put_cut_caption_without_ai_notice_is_still_accepted(owner: TestClient) -> None:
    body = package()
    body["cuts"][0]["caption"] = "No notice here."
    assert owner.put(DELIVERY, json=body).status_code == 200


def test_record_post_without_text_keeps_the_earlier_posts_text(owner: TestClient) -> None:
    owner.put(DELIVERY, json=package())
    owner.put(POST_ON_TIKTOK, json=post_with(caption="Feito com IA no TikTok."))
    owner.put(POST_ON_TIKTOK, json={"cut": "main", "url": "https://tiktok.test/v/2"})
    (post,) = owner.get(DELIVERY).json()["posts"]
    assert post["caption"] == "Feito com IA no TikTok."


def test_record_post_with_empty_text_returns_to_the_cuts_text(owner: TestClient) -> None:
    owner.put(DELIVERY, json=package())
    owner.put(POST_ON_TIKTOK, json=post_with(caption="Feito com IA no TikTok."))
    owner.put(POST_ON_TIKTOK, json=post_with(caption=""))
    (post,) = owner.get(DELIVERY).json()["posts"]
    assert post["caption"] == ""


def test_delivery_get_post_falling_back_to_cut_without_notice_warns(owner: TestClient) -> None:
    body = package()
    body["cuts"][0]["caption"] = "No notice here."
    owner.put(DELIVERY, json=body)
    owner.put(POST_ON_TIKTOK, json=post_with())
    kinds = {item["kind"] for item in owner.get(DELIVERY).json()["warnings"]}
    assert DeliveryWarningKind.POST_NO_AI_NOTICE.value in kinds


def test_delivery_get_post_falling_back_to_cut_with_many_hashtags_warns(
    owner: TestClient,
) -> None:
    body = package()
    body["cuts"][0]["hashtags"] = [f"tag{number}" for number in range(MAX_POST_HASHTAGS + 1)]
    owner.put(DELIVERY, json=body)
    owner.put(POST_ON_TIKTOK, json=post_with())
    kinds = {item["kind"] for item in owner.get(DELIVERY).json()["warnings"]}
    assert DeliveryWarningKind.POST_TOO_MANY_HASHTAGS.value in kinds


def test_delivery_get_post_with_own_valid_text_does_not_warn_about_text(owner: TestClient) -> None:
    body = package()
    body["cuts"][0]["caption"] = "No notice here."
    owner.put(DELIVERY, json=body)
    owner.put(POST_ON_TIKTOK, json=post_with(caption="Feito com IA.", hashtags=["a"]))
    kinds = {item["kind"] for item in owner.get(DELIVERY).json()["warnings"]}
    assert DeliveryWarningKind.POST_NO_AI_NOTICE.value not in kinds


def test_episode_put_published_warns_about_post_text_without_notice(owner: TestClient) -> None:
    body = package()
    body["cuts"][0]["caption"] = "No notice here."
    owner.put(DELIVERY, json=body)
    owner.put(POST_ON_TIKTOK, json=post_with())
    response = owner.put(EPISODE, json=episode_body("published"))
    assert DeliveryWarningKind.POST_NO_AI_NOTICE.value in {
        item["kind"] for item in response.json()["warnings"]
    }


# Published warnings


def episode_body(status: str, **extra: Any) -> dict[str, Any]:
    return {"title": "Pilot", "summary": "", "status": status, **extra}


def test_episode_put_published_with_gaps_succeeds_and_warns(owner: TestClient) -> None:
    owner.put(DELIVERY, json=package())
    response = owner.put(EPISODE, json=episode_body("published"))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "published"
    assert [item["kind"] for item in response.json()["warnings"]] == ["no_video"]


def test_episode_put_published_without_delivery_has_no_warnings(owner: TestClient) -> None:
    response = owner.put(EPISODE, json=episode_body("published"))
    assert response.status_code == 200 and response.json()["warnings"] == []


def test_episode_put_other_status_has_no_warnings(owner: TestClient) -> None:
    owner.put(DELIVERY, json=package())
    response = owner.put(EPISODE, json=episode_body("script"))
    assert response.json()["warnings"] == []


# MCP


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def test_mcp_write_delivery_then_get_returns_cuts(mcp: TestClient) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "delivery": package(), "expected_revision": 0}
    tool(mcp, "write_delivery", arguments)
    found = tool(mcp, "get_delivery", {"slug": "funny-memes", "number": 1})
    assert found["structuredContent"]["cuts"][0]["name"] == "main"


def test_mcp_write_delivery_stale_revision_names_the_retry(mcp: TestClient) -> None:
    arguments = {"slug": "funny-memes", "number": 1, "delivery": package()}
    tool(mcp, "write_delivery", arguments)
    result = tool(mcp, "write_delivery", {**arguments, "expected_revision": 0})
    assert result["isError"] and "expected_revision=1" in result["content"][0]["text"]


def test_mcp_write_delivery_bad_video_names_the_fixing_call(mcp: TestClient) -> None:
    bad = package(video_id="11111111-1111-1111-1111-111111111111")
    result = tool(mcp, "write_delivery", {"slug": "funny-memes", "number": 1, "delivery": bad})
    assert result["isError"] and "list_media" in result["content"][0]["text"]


def test_mcp_record_post_after_cut_returns_package_with_post(mcp: TestClient) -> None:
    tool(mcp, "write_delivery", {"slug": "funny-memes", "number": 1, "delivery": package()})
    post = {"platform": "tiktok", "cut": "main", "url": "https://tiktok.test/v/1"}
    result = tool(mcp, "record_post", {"slug": "funny-memes", "number": 1, "post": post})
    assert result["structuredContent"]["posts"][0]["platform"] == "tiktok"


def test_mcp_record_post_with_same_key_replays_the_first_result(mcp: TestClient) -> None:
    tool(mcp, "write_delivery", {"slug": "funny-memes", "number": 1, "delivery": package()})
    arguments = {
        "slug": "funny-memes",
        "number": 1,
        "post": {"platform": "tiktok", "cut": "main"},
        "idempotency_key": "post-1",
    }
    first = tool(mcp, "record_post", arguments)
    again = tool(mcp, "record_post", arguments)
    assert first["structuredContent"]["revision"] == again["structuredContent"]["revision"]


def test_mcp_record_post_without_cut_tells_to_write_it(mcp: TestClient) -> None:
    post = {"platform": "tiktok", "cut": "ghost"}
    result = tool(mcp, "record_post", {"slug": "funny-memes", "number": 1, "post": post})
    assert result["isError"] and "write_delivery" in result["content"][0]["text"]


def test_mcp_record_post_with_own_text_returns_it(mcp: TestClient) -> None:
    tool(mcp, "write_delivery", {"slug": "funny-memes", "number": 1, "delivery": package()})
    post = {"platform": "tiktok", "cut": "main", "caption": "Feito com IA.", "hashtags": ["#a"]}
    result = tool(mcp, "record_post", {"slug": "funny-memes", "number": 1, "post": post})
    assert result["structuredContent"]["posts"][0]["hashtags"] == ["a"]


def test_mcp_write_delivery_post_caption_without_notice_names_the_field(mcp: TestClient) -> None:
    body = package()
    body["posts"] = [{"platform": "tiktok", "cut": "main", "caption": "Just a caption."}]
    result = tool(mcp, "write_delivery", {"slug": "funny-memes", "number": 1, "delivery": body})
    text = result["content"][0]["text"]
    assert result["isError"] and "posts.0.caption" in text and "AI notice" in text


def test_mcp_record_post_with_too_many_hashtags_is_a_tool_error(mcp: TestClient) -> None:
    tool(mcp, "write_delivery", {"slug": "funny-memes", "number": 1, "delivery": package()})
    tags = [f"tag{number}" for number in range(MAX_POST_HASHTAGS + 1)]
    post = {"platform": "tiktok", "cut": "main", "hashtags": tags}
    result = tool(mcp, "record_post", {"slug": "funny-memes", "number": 1, "post": post})
    assert result["isError"] and "hashtags" in result["content"][0]["text"]


def test_mcp_delivery_tools_declare_all_four_annotations(mcp: TestClient) -> None:
    from tests.test_agentic import mcp_call

    tools = mcp_call(mcp, "tools/list", {})["result"]["tools"]
    wanted = {"get_delivery", "write_delivery", "record_post"}
    found = [item for item in tools if item["name"] in wanted]
    assert len(found) == len(wanted)
    keys = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
    assert all(keys <= item["annotations"].keys() for item in found)


def test_mcp_write_delivery_blocked_by_gate_returns_tool_error(
    app: FastAPI, owner: TestClient, app_settings: Any
) -> None:
    block(app_settings, "/funny-memes", "delivery.write")
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        result = tool(
            client, "write_delivery", {"slug": "funny-memes", "number": 1, "delivery": package()}
        )
    assert result["isError"]
