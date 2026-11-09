"""The delivery page, a post's numbers page and the channel's comparison page."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from tests import media_samples as sample
from tests.conftest import RecordingMailer
from tests.workspace_helpers import flash_after, join, member, new_channel, submit

CHANNEL = "/app/channels/funny-memes"
EPISODE = f"{CHANNEL}/episodes/1"
DELIVERY = f"{EPISODE}/delivery"
METRICS = f"{DELIVERY}/metrics/tiktok"
COMPARE = f"{CHANNEL}/metrics"
MEDIA = f"{EPISODE}/media"
CSV = "second,percent\n0,100\n1,90\n"


@pytest.fixture
def owner(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    client = member(app, mailer, "ana@example.com")
    new_channel(client)
    submit(client, f"{CHANNEL}/accounts", {"platform": "tiktok", "handle": "@funnymemes"})
    created = submit(client, f"{CHANNEL}/episodes", {"title": "Pilot"})
    client.get(created.headers["location"])
    return client


def dialog_ids(client: TestClient) -> set[str]:
    """The ids of the media files, as the edit buttons of the media page carry them."""
    return set(re.findall(r'data-dialog="edit-([0-9a-f-]{36})"', client.get(MEDIA).text))


def media_id(client: TestClient, kind_path: str, content: bytes, name: str) -> str:
    """Upload a file through the media page and return its id."""
    before = dialog_ids(client)
    sent = submit(
        client, f"{MEDIA}/upload/{kind_path}", {}, files={"file": (name, content, "application/x")}
    )
    assert sent.status_code == 303, sent.text
    (new,) = dialog_ids(client) - before
    return new


def add_cut(client: TestClient, **fields: str) -> Response:
    data = {
        "name": "main",
        "caption": "Oi! Feito com IA.",
        "hashtags": "#dogs funny",
        "revision": "0",
        **fields,
    }
    return submit(client, f"{DELIVERY}/cuts", data)


@pytest.fixture
def with_cut(owner: TestClient) -> TestClient:
    owner.get(add_cut(owner).headers["location"])
    return owner


def set_post(client: TestClient, **fields: str) -> Response:
    revision = re.search(r'name="revision" value="(\d+)"', client.get(DELIVERY).text)
    assert revision
    data = {
        "cut": "main",
        "url": "https://tiktok.test/v/1",
        "revision": revision.group(1),
        **fields,
    }
    return submit(client, f"{DELIVERY}/posts/tiktok", data)


@pytest.fixture
def posted(with_cut: TestClient) -> TestClient:
    with_cut.get(set_post(with_cut).headers["location"])
    return with_cut


# Delivery page


def test_episode_page_links_to_delivery_tab(owner: TestClient) -> None:
    assert f'href="{DELIVERY}"' in owner.get(EPISODE).text


def test_delivery_page_without_cuts_shows_empty_state(owner: TestClient) -> None:
    assert "No cuts yet." in owner.get(DELIVERY).text


def test_delivery_add_cut_shows_note_name_caption_and_hashtags(owner: TestClient) -> None:
    page = flash_after(owner, add_cut(owner))
    assert "Changes saved." in page and "<strong>main</strong>" in page
    assert "Oi!" in page and "#dogs" in page and "#funny" in page


def test_delivery_add_cut_without_video_warns_on_the_page(owner: TestClient) -> None:
    page = flash_after(owner, add_cut(owner))
    assert "The package is incomplete" in page and "The cut main has no final video." in page


def test_delivery_add_cut_with_existing_name_shows_error_in_open_dialog(
    with_cut: TestClient,
) -> None:
    response = add_cut(with_cut, revision="1")
    assert response.status_code == 422 and "A cut with this name already exists." in response.text


def test_delivery_add_cut_with_bad_hashtag_shows_error_next_to_field(owner: TestClient) -> None:
    response = add_cut(owner, hashtags="a-b")
    assert response.status_code == 422 and 'id="new-cut-hashtags-error"' in response.text


def test_delivery_add_cut_with_bad_name_shows_error_next_to_field(owner: TestClient) -> None:
    response = add_cut(owner, name="Not A Slug")
    assert response.status_code == 422 and 'id="new-cut-name-error"' in response.text


def test_delivery_add_cut_with_stale_revision_conflicts(with_cut: TestClient) -> None:
    response = add_cut(with_cut, name="vertical", revision="0")
    assert response.status_code == 409 and "Someone changed the delivery package" in response.text


def test_delivery_add_cut_with_foreign_video_id_is_refused(owner: TestClient) -> None:
    response = add_cut(owner, video_id="11111111-1111-1111-1111-111111111111")
    assert response.status_code == 422 and "Choose a video file of this episode." in response.text


def test_delivery_add_cut_with_uploaded_video_and_cover_shows_players(owner: TestClient) -> None:
    video = media_id(owner, "visual-effects", sample.MP4, "final.mp4")
    cover = media_id(owner, "covers", sample.PNG, "cover.png")
    page = flash_after(owner, add_cut(owner, video_id=video, cover_id=cover))
    assert "<video" in page and "<img" in page and "The package is incomplete" not in page


def test_delivery_save_cut_changes_caption(with_cut: TestClient) -> None:
    response = submit(with_cut, f"{DELIVERY}/cuts/main", {"caption": "Tchau!", "revision": "1"})
    assert "Tchau!" in flash_after(with_cut, response)


def test_delivery_remove_cut_takes_it_out(with_cut: TestClient) -> None:
    response = submit(with_cut, f"{DELIVERY}/cuts/main/remove", {"revision": "1"})
    assert "No cuts yet." in flash_after(with_cut, response)


def test_delivery_remove_cut_used_by_a_post_is_refused(posted: TestClient) -> None:
    response = submit(posted, f"{DELIVERY}/cuts/main/remove", {"revision": "2"})
    assert response.status_code == 422 and "not in this package" in response.text


def test_delivery_set_post_shows_link_and_numbers_button(with_cut: TestClient) -> None:
    page = flash_after(with_cut, set_post(with_cut, published_at="2026-11-07T10:30"))
    assert 'href="https://tiktok.test/v/1"' in page and "2026-11-07 10:30 UTC" in page
    assert f'href="{METRICS}"' in page


def test_delivery_set_post_without_link_says_so_and_warns(with_cut: TestClient) -> None:
    page = flash_after(with_cut, set_post(with_cut, url=""))
    assert "No link yet." in page and "The post on tiktok has no link." in page


def test_delivery_set_post_with_bad_link_shows_error_next_to_field(with_cut: TestClient) -> None:
    response = set_post(with_cut, url="ftp://x")
    assert response.status_code == 422 and 'id="tiktok-url-error"' in response.text


def test_delivery_set_post_on_unknown_platform_is_not_found(with_cut: TestClient) -> None:
    assert submit(with_cut, f"{DELIVERY}/posts/nope", {"cut": "main"}).status_code == 404


def test_delivery_set_post_on_platform_without_account_shows_form_error(
    with_cut: TestClient,
) -> None:
    response = submit(with_cut, f"{DELIVERY}/posts/youtube", {"cut": "main", "revision": "1"})
    assert response.status_code == 422


def test_delivery_set_post_with_own_text_shows_it_on_the_page(with_cut: TestClient) -> None:
    page = flash_after(
        with_cut, set_post(with_cut, caption="Para o TikTok. Feito com IA.", hashtags="#cao")
    )
    assert "Para o TikTok." in page and "#cao" in page


def test_delivery_set_post_without_own_text_says_it_uses_the_cuts(with_cut: TestClient) -> None:
    page = flash_after(with_cut, set_post(with_cut))
    assert "the post uses the text of its cut." in page


def test_delivery_set_post_caption_without_notice_shows_error_next_to_field(
    with_cut: TestClient,
) -> None:
    response = set_post(with_cut, caption="Just a caption.")
    assert response.status_code == 422 and 'id="tiktok-caption-error"' in response.text
    assert "needs an AI notice" in response.text


def test_delivery_set_post_with_too_many_hashtags_shows_error_next_to_field(
    with_cut: TestClient,
) -> None:
    response = set_post(with_cut, hashtags="#a #b #c #d #e #f")
    assert response.status_code == 422 and 'id="tiktok-hashtags-error"' in response.text
    assert "Use at most 5 hashtags." in response.text


def test_delivery_set_post_caption_error_is_translated_to_portuguese(with_cut: TestClient) -> None:
    with_cut.get("/", params={"lang": "pt-BR"})
    response = set_post(with_cut, caption="Just a caption.")
    assert "A legenda precisa de um aviso de IA" in response.text


def test_delivery_remove_post_takes_it_out(posted: TestClient) -> None:
    response = submit(posted, f"{DELIVERY}/posts/tiktok/remove", {"revision": "2"})
    assert "Not posted yet." in flash_after(posted, response)


def test_delivery_page_in_portuguese_is_translated(with_cut: TestClient) -> None:
    page = with_cut.get(f"{DELIVERY}?lang=pt-BR").text
    assert "Cortes" in page and "O corte main não tem vídeo final." in page


def test_delivery_page_for_viewer_hides_actions_and_refuses_posts(
    app: FastAPI, mailer: RecordingMailer, with_cut: TestClient
) -> None:
    viewer = join(app, mailer, with_cut, "bia@example.com", "viewer")
    page = viewer.get(DELIVERY).text
    assert "<strong>main</strong>" in page and "Add cut" not in page
    assert add_cut(viewer, name="other").status_code == 403


def test_delivery_page_of_unknown_episode_is_not_found(owner: TestClient) -> None:
    assert owner.get(f"{CHANNEL}/episodes/9/delivery").status_code == 404


def test_delivery_page_without_session_redirects_to_sign_in(app: FastAPI) -> None:
    from tests.workspace_helpers import browser

    assert browser(app).get(DELIVERY).status_code == 303


# Published


def test_episode_save_published_with_gaps_saves_and_says_the_package_is_incomplete(
    with_cut: TestClient,
) -> None:
    response = submit(
        with_cut, EPISODE, {"title": "Pilot", "summary": "", "status": "published", "revision": "1"}
    )
    assert "its delivery package is incomplete" in flash_after(with_cut, response)


def test_episode_save_published_complete_says_changes_saved(owner: TestClient) -> None:
    response = submit(
        owner, EPISODE, {"title": "Pilot", "summary": "", "status": "published", "revision": "1"}
    )
    assert "Changes saved." in flash_after(owner, response)


def test_board_move_to_published_with_gaps_says_the_package_is_incomplete(
    with_cut: TestClient,
) -> None:
    response = submit(with_cut, f"{EPISODE}/move", {"status": "published"})
    assert "its delivery package is incomplete" in flash_after(with_cut, response)


# Numbers page


def test_metrics_page_of_post_not_in_package_is_not_found(with_cut: TestClient) -> None:
    assert with_cut.get(METRICS).status_code == 404


def test_metrics_page_without_numbers_shows_empty_state(posted: TestClient) -> None:
    assert "No numbers yet." in posted.get(METRICS).text


def test_metrics_add_numbers_shows_the_day_and_values(posted: TestClient) -> None:
    response = submit(
        posted, f"{METRICS}/snapshots", {"captured_on": "2026-11-01", "views": "120", "likes": "7"}
    )
    page = flash_after(posted, response)
    assert "Numbers recorded." in page and "Nov 1, 2026" in page
    assert "Views 120 · Likes 7" in page


def test_metrics_add_numbers_with_negative_value_shows_error_next_to_field(
    posted: TestClient,
) -> None:
    response = submit(posted, f"{METRICS}/snapshots", {"views": "-3"})
    assert response.status_code == 422 and 'id="numbers-views-error"' in response.text


def test_metrics_add_numbers_with_text_shows_error_next_to_field(posted: TestClient) -> None:
    response = submit(posted, f"{METRICS}/snapshots", {"reach": "many"})
    assert response.status_code == 422 and 'id="numbers-reach-error"' in response.text


def test_metrics_import_csv_shows_retention_points(posted: TestClient) -> None:
    response = submit(
        posted,
        f"{METRICS}/retention",
        {"captured_on": "2026-11-01"},
        files={"file": ("retention.csv", CSV.encode(), "text/csv")},
    )
    assert "Retention (2 points)" in flash_after(posted, response)


def test_metrics_import_bad_csv_shows_first_problem_next_to_file_field(posted: TestClient) -> None:
    response = submit(
        posted,
        f"{METRICS}/retention",
        {},
        files={"file": ("retention.csv", b"second,percent\n0,oops\n", "text/csv")},
    )
    assert response.status_code == 422 and "Row 2: percent must be a number." in response.text


def test_metrics_import_bad_csv_in_portuguese_is_translated(posted: TestClient) -> None:
    response = submit(
        posted,
        f"{METRICS}/retention?lang=pt-BR",
        {},
        files={"file": ("retention.csv", b"second,percent\n0,oops\n", "text/csv")},
    )
    assert "Linha 2: percent precisa ser um número." in response.text


def test_metrics_import_without_file_shows_error(posted: TestClient) -> None:
    response = submit(posted, f"{METRICS}/retention", {})
    assert response.status_code == 422


def test_metrics_forms_for_viewer_are_hidden_and_refused(
    app: FastAPI, mailer: RecordingMailer, posted: TestClient
) -> None:
    viewer = join(app, mailer, posted, "bia@example.com", "viewer")
    assert "Enter numbers" not in viewer.get(METRICS).text
    assert submit(viewer, f"{METRICS}/snapshots", {"views": "1"}).status_code == 403


# Comparison page


def test_compare_page_without_posts_shows_empty_state(owner: TestClient) -> None:
    assert "No posts yet." in owner.get(COMPARE).text


def test_compare_page_lists_post_with_its_latest_numbers(posted: TestClient) -> None:
    submit(posted, f"{METRICS}/snapshots", {"views": "42"})
    page = posted.get(COMPARE).text
    assert "#1 · Pilot" in page and "Views 42" in page


def test_compare_page_post_without_numbers_says_so(posted: TestClient) -> None:
    assert "No numbers yet." in posted.get(COMPARE).text


def test_compare_page_sorting_marks_the_chosen_number(posted: TestClient) -> None:
    page = posted.get(f"{COMPARE}?sort=likes").text
    assert re.search(r'aria-current="page">Likes</a>', page)


def test_compare_page_is_in_the_channel_menu(owner: TestClient) -> None:
    assert f'href="{COMPARE}"' in owner.get(CHANNEL).text


def test_compare_page_in_portuguese_is_translated(posted: TestClient) -> None:
    assert "Visualizações" in posted.get(f"{COMPARE}?lang=pt-BR").text


# Covers on the media page


def test_media_page_offers_covers_with_image_preview(owner: TestClient) -> None:
    cover = media_id(owner, "covers", sample.PNG, "cover.png")
    page = owner.get(MEDIA).text
    assert "Upload cover" in page and '<img class="player"' in page and cover in page


def test_media_upload_cover_with_audio_is_refused(owner: TestClient) -> None:
    sent = submit(
        owner,
        f"{MEDIA}/upload/covers",
        {},
        files={"file": ("take.mp3", sample.MP3_TAGGED, "audio/mpeg")},
    )
    assert sent.status_code == 422
