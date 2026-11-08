"""Full-text search over a channel's writing: indexing, freshness, isolation, API and MCP."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.content_search import ContentSearchStore, match_expression
from sreditor.database import Database
from sreditor.settings import AppSettings
from tests.conftest import forget_schema_version
from tests.test_agentic import PUBLIC_URL, agent, new_token, tool
from tests.test_api import FUNNY_MEMES
from tests.test_migrations import fill, old_style_database

SLUG = "funny-memes"
CHANNEL = f"/channels/{SLUG}"
EPISODE = f"{CHANNEL}/episodes/1"
SEARCH = f"{CHANNEL}/search"
NASA = {
    "kind": "video",
    "title": "Zebra migration footage",
    "url": "https://svs.gsfc.nasa.gov/zebra",
    "creator": "NASA",
    "rights": "public_domain",
}
STUDY = {
    "url": "https://example.com/study",
    "title": "Zebra study",
    "claim": "Zebras sleep standing up.",
    "accessed_on": "2026-11-01",
}
SCRIPT = {
    "duration_s": 5,
    "voice": {"voice_id": "v1", "model": "v3", "language": "pt-BR"},
    "blocks": [{"start_s": 0, "end_s": 5, "function": "hook", "speech": "Meet the zebra."}],
    "screen_texts": [{"text": "Giraffe cameo"}],
}
READ_ONLY = ["channels:read"]
OTHER_CHANNEL = {
    "slug": "other",
    "name": "Other",
    "accounts": [{"platform": "youtube", "handle": "@other", "description": "Other."}],
}


@pytest.fixture
def owner(sign_up: Callable[..., TestClient]) -> TestClient:
    """Signed in as the owner of funny-memes, which has one episode, Pilot."""
    client = sign_up("owner@example.com", "Owner")
    assert client.post("/channels", json=FUNNY_MEMES).status_code == 201
    assert client.post(f"{CHANNEL}/episodes", json={"title": "Pilot"}).status_code == 201
    return client


@pytest.fixture
def mcp(app: FastAPI, owner: TestClient) -> Iterator[TestClient]:
    with agent(app, new_token(owner), PUBLIC_URL) as client:
        yield client


def hits(client: TestClient, words: str, **extra: Any) -> list[dict[str, Any]]:
    """The results of a search of funny-memes."""
    response = client.get(SEARCH, params={"q": words, **extra})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def kinds(client: TestClient, words: str) -> set[str]:
    """The kinds of the results for the words."""
    return {item["kind"] for item in hits(client, words)}


def created(client: TestClient, path: str, body: dict[str, Any]) -> str:
    """Create something and return its id."""
    response = client.post(f"{CHANNEL}/{path}", json=body)
    assert response.status_code == 201, response.text
    found: str = response.json()["id"]
    return found


def put_ok(client: TestClient, path: str, body: dict[str, Any]) -> None:
    """Replace something, which must succeed."""
    response = client.put(f"{CHANNEL}/{path}", json=body)
    assert response.status_code == 200, response.text


# One way to write each kind of text, each with a word only it holds (rule project.search).
def write_episode_title(client: TestClient) -> None:
    put_ok(client, "episodes/1", {"title": "Zebra special"})


def write_episode_summary(client: TestClient) -> None:
    put_ok(client, "episodes/1", {"title": "Pilot", "summary": "About a zebra."})


def write_research(client: TestClient) -> None:
    put_ok(client, "episodes/1/research", {"text": "Zebra stripes are unique."})


def write_source(client: TestClient) -> None:
    created(client, "episodes/1/sources", STUDY)


def write_screenplay(client: TestClient) -> None:
    put_ok(client, "episodes/1/screenplay", {"text": "INT. ZEBRA PEN - DAY\n\nA zebra waits."})


def write_short_script(client: TestClient) -> None:
    put_ok(client, "episodes/1/short-script", SCRIPT)


def write_pitch(client: TestClient) -> None:
    created(client, "pitches", {"title": "Zebra pitch", "summary": "Stripes."})


def write_pitch_notes(client: TestClient) -> None:
    pitch = created(client, "pitches", {"title": "Plain pitch"})
    put_ok(client, f"pitches/{pitch}/research", {"text": "Zebra herds migrate."})


def write_pitch_source(client: TestClient) -> None:
    pitch = created(client, "pitches", {"title": "Plain pitch"})
    created(client, f"pitches/{pitch}/sources", STUDY)


def write_character(client: TestClient) -> None:
    created(client, "characters", {"name": "ZEBRA", "description": "A striped host."})


def write_location(client: TestClient) -> None:
    created(client, "locations", {"name": "ZEBRA PEN", "description": "Dusty."})


def write_prop(client: TestClient) -> None:
    created(client, "props", {"name": "Zebra mask", "description": "Striped."})


def write_style(client: TestClient) -> None:
    created(client, "styles", {"name": "Zebra look", "description": "Black and white."})


def write_external_media(client: TestClient) -> None:
    created(client, "external-media", NASA)


def write_context(client: TestClient) -> None:
    put_ok(client, "items/episode/1/context", {"what": "A zebra fakes a sick day."})


WRITERS: dict[str, Callable[[TestClient], None]] = {
    "episode": write_episode_title,
    "episode-summary": write_episode_summary,
    "research": write_research,
    "source": write_source,
    "screenplay": write_screenplay,
    "short_script": write_short_script,
    "pitch": write_pitch,
    "pitch_notes": write_pitch_notes,
    "pitch_source": write_pitch_source,
    "character": write_character,
    "location": write_location,
    "prop": write_prop,
    "style": write_style,
    "external_media": write_external_media,
    "context": write_context,
}
ALL_KINDS = pytest.mark.parametrize("name", list(WRITERS))


def expected_kind(name: str) -> str:
    """The kind a writer's text is found as; the episode's summary is still the episode."""
    return "episode" if name == "episode-summary" else name


# Indexing on create


@ALL_KINDS
def test_search_channel_new_text_is_found_as_its_kind(owner: TestClient, name: str) -> None:
    WRITERS[name](owner)
    assert expected_kind(name) in kinds(owner, "zebra")


def test_search_channel_short_script_screen_text_is_found(owner: TestClient) -> None:
    write_short_script(owner)
    assert kinds(owner, "giraffe") == {"short_script"}


def test_search_channel_short_script_json_keys_are_not_text(owner: TestClient) -> None:
    write_short_script(owner)
    assert hits(owner, "speech") == []


def test_search_channel_ignores_case_and_accents(owner: TestClient) -> None:
    put_ok(owner, "episodes/1/research", {"text": "Produção Ágil"})
    assert kinds(owner, "producao AGIL") == {"research"}


def test_search_channel_last_word_matches_a_prefix(owner: TestClient) -> None:
    put_ok(owner, "episodes/1/research", {"text": "Mondays are hated"})
    assert kinds(owner, "mond") == {"research"}


def test_search_channel_needs_every_word(owner: TestClient) -> None:
    put_ok(owner, "episodes/1/research", {"text": "Mondays are hated"})
    assert hits(owner, "mondays loved") == []


# Freshness: update and delete


def test_search_channel_rewritten_research_drops_old_words(owner: TestClient) -> None:
    put_ok(owner, "episodes/1/research", {"text": "Zebra stripes"})
    put_ok(owner, "episodes/1/research", {"text": "Giraffe necks"})
    assert (hits(owner, "zebra"), kinds(owner, "giraffe")) == ([], {"research"})


def test_search_channel_renamed_episode_is_found_by_new_title_only(owner: TestClient) -> None:
    write_episode_title(owner)
    put_ok(owner, "episodes/1", {"title": "Giraffe special"})
    assert (hits(owner, "zebra"), kinds(owner, "giraffe")) == ([], {"episode"})


def test_search_channel_rewritten_screenplay_drops_old_words(owner: TestClient) -> None:
    write_screenplay(owner)
    put_ok(owner, "episodes/1/screenplay", {"text": "INT. ROOM - DAY\n\nA giraffe waits."})
    assert (hits(owner, "zebra"), kinds(owner, "giraffe")) == ([], {"screenplay"})


def test_search_channel_rewritten_short_script_drops_old_words(owner: TestClient) -> None:
    write_short_script(owner)
    blocks = [{**SCRIPT["blocks"][0], "speech": "Meet the llama."}]  # type: ignore[index]
    put_ok(owner, "episodes/1/short-script", {**SCRIPT, "blocks": blocks, "screen_texts": []})
    assert (hits(owner, "zebra"), kinds(owner, "llama")) == ([], {"short_script"})


def test_search_channel_rewritten_pitch_notes_drop_old_words(owner: TestClient) -> None:
    pitch = created(owner, "pitches", {"title": "Plain"})
    put_ok(owner, f"pitches/{pitch}/research", {"text": "Zebra herds"})
    put_ok(owner, f"pitches/{pitch}/research", {"text": "Llama herds"})
    assert (hits(owner, "zebra"), kinds(owner, "llama")) == ([], {"pitch_notes"})


def test_search_channel_changed_pitch_title_is_reindexed(owner: TestClient) -> None:
    pitch = created(owner, "pitches", {"title": "Zebra pitch"})
    put_ok(owner, f"pitches/{pitch}", {"title": "Llama pitch"})
    assert (hits(owner, "zebra"), kinds(owner, "llama")) == ([], {"pitch"})


def test_search_channel_renamed_character_is_reindexed(owner: TestClient) -> None:
    entry = created(owner, "characters", {"name": "ZEBRA", "description": ""})
    put_ok(owner, f"characters/{entry}", {"name": "LLAMA", "description": ""})
    assert (hits(owner, "zebra"), kinds(owner, "llama")) == ([], {"character"})


def test_search_channel_replaced_third_party_media_is_reindexed(owner: TestClient) -> None:
    item = created(owner, "external-media", NASA)
    put_ok(owner, f"external-media/{item}", {**NASA, "title": "Llama footage"})
    assert (hits(owner, "zebra"), kinds(owner, "llama")) == ([], {"external_media"})


def test_search_channel_changed_context_is_reindexed(owner: TestClient) -> None:
    write_context(owner)
    put_ok(owner, "items/episode/1/context", {"what": "A llama fakes a sick day."})
    assert (hits(owner, "zebra"), kinds(owner, "llama")) == ([], {"context"})


def test_search_channel_deleted_episode_takes_all_its_text(owner: TestClient) -> None:
    for write in (write_research, write_screenplay, write_short_script, write_source):
        write(owner)
    write_context(owner)
    assert owner.delete(EPISODE).status_code == 204
    assert hits(owner, "zebra") == []


def test_search_channel_deleted_pitch_takes_its_notes_and_sources(owner: TestClient) -> None:
    pitch = created(owner, "pitches", {"title": "Zebra pitch"})
    put_ok(owner, f"pitches/{pitch}/research", {"text": "Zebra herds"})
    created(owner, f"pitches/{pitch}/sources", STUDY)
    assert owner.delete(f"{CHANNEL}/pitches/{pitch}").status_code == 204
    assert hits(owner, "zebra") == []


def test_search_channel_deleted_source_takes_its_context(owner: TestClient) -> None:
    source = created(owner, "episodes/1/sources", STUDY)
    put_ok(owner, f"items/source/{source}/context", {"what": "Zebra facts"})
    assert owner.delete(f"{EPISODE}/sources/{source}").status_code == 204
    assert hits(owner, "zebra") == []


def test_search_channel_deleted_library_entry_takes_its_context(owner: TestClient) -> None:
    entry = created(owner, "characters", {"name": "ZEBRA", "description": ""})
    put_ok(owner, f"items/character/{entry}/context", {"who": "Zebra host"})
    assert owner.delete(f"{CHANNEL}/characters/{entry}").status_code == 204
    assert hits(owner, "zebra") == []


def test_search_channel_deleted_third_party_media_is_forgotten(owner: TestClient) -> None:
    item = created(owner, "external-media", NASA)
    assert owner.delete(f"{CHANNEL}/external-media/{item}").status_code == 204
    assert hits(owner, "zebra") == []


def test_search_channel_deleted_channel_leaves_no_entries(
    owner: TestClient, app_settings: AppSettings
) -> None:
    write_research(owner)
    write_character(owner)
    assert owner.delete(CHANNEL).status_code == 204
    with closing(sqlite3.connect(app_settings.channels_db)) as connection:
        entries = connection.execute("SELECT COUNT(*) FROM search_entries").fetchone()[0]
        index = connection.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
    assert (entries, index) == (0, 0)


# What the index never holds


def test_search_index_has_no_trigger_on_waivers(
    owner: TestClient, app_settings: AppSettings
) -> None:
    with closing(sqlite3.connect(app_settings.channels_db)) as connection:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            " AND tbl_name = 'episode_waivers' AND name LIKE 'search_%'"
        ).fetchall()
    assert triggers == []


# Isolation


def test_search_channel_results_stay_in_their_channel(owner: TestClient) -> None:
    write_research(owner)
    assert owner.post("/channels", json=OTHER_CHANNEL).status_code == 201
    assert owner.post("/channels/other/episodes", json={"title": "Other pilot"}).status_code == 201
    elsewhere = "/channels/other/episodes/1/research"
    assert owner.put(elsewhere, json={"text": "Zebra elsewhere"}).status_code == 200
    mine = owner.get(SEARCH, params={"q": "zebra"}).json()["items"]
    theirs = owner.get("/channels/other/search", params={"q": "zebra"}).json()["items"]
    assert [item["snippet"] for item in mine] == ["«Zebra» stripes are unique."]
    assert [item["snippet"] for item in theirs] == ["«Zebra» elsewhere"]


def test_search_channel_of_another_users_channel_answers_404(
    sign_up: Callable[..., TestClient], owner: TestClient
) -> None:
    write_research(owner)
    stranger = sign_up("stranger@example.com", "Stranger")
    assert stranger.get(SEARCH, params={"q": "zebra"}).status_code == 404


def test_search_channel_token_limited_to_other_channels_answers_404(
    app: FastAPI, owner: TestClient
) -> None:
    write_research(owner)
    assert owner.post("/channels", json=OTHER_CHANNEL).status_code == 201
    limited = agent(app, new_token(owner, channels=["other"]))
    assert limited.get(SEARCH, params={"q": "zebra"}).status_code == 404


def test_search_channel_token_without_read_scope_answers_403(
    app: FastAPI, owner: TestClient
) -> None:
    limited = agent(app, new_token(owner, ["editorial:write"]))
    assert limited.get(SEARCH, params={"q": "zebra"}).status_code == 403


def test_search_channel_token_with_read_scope_finds_text(app: FastAPI, owner: TestClient) -> None:
    write_research(owner)
    reader = agent(app, new_token(owner, READ_ONLY))
    assert kinds(reader, "zebra") == {"research"}


# Results


def test_search_channel_result_names_kind_id_place_title_snippet_and_opening(
    owner: TestClient,
) -> None:
    write_research(owner)
    (found,) = hits(owner, "zebra")
    assert found == {
        "kind": "research",
        "id": "1",
        "episode": 1,
        "pitch_id": None,
        "title": "Pilot",
        "snippet": "«Zebra» stripes are unique.",
        "open_with": {
            "tool": "get_research",
            "arguments": {"slug": SLUG, "number": 1},
            "api_path": f"{EPISODE}/research",
        },
    }


def test_search_channel_pitch_result_names_the_pitch(owner: TestClient) -> None:
    pitch = created(owner, "pitches", {"title": "Zebra pitch"})
    (found,) = hits(owner, "zebra")
    assert (found["pitch_id"], found["episode"], found["id"]) == (pitch, None, pitch)


def test_search_channel_pitch_source_result_falls_under_its_pitch(owner: TestClient) -> None:
    write_pitch_source(owner)
    (found,) = hits(owner, "zebra")
    assert found["kind"] == "pitch_source" and found["title"] == "Zebra study"
    assert found["open_with"]["tool"] == "get_pitch"


def test_search_channel_context_of_source_names_its_episode(owner: TestClient) -> None:
    source = created(owner, "episodes/1/sources", {**STUDY, "title": "Plain", "claim": "Plain"})
    put_ok(owner, f"items/source/{source}/context", {"what": "Zebra facts"})
    (found,) = hits(owner, "zebra")
    assert (found["kind"], found["episode"], found["id"]) == ("context", 1, f"source:{source}")


@ALL_KINDS
def test_search_channel_opening_names_a_call_that_works(
    owner: TestClient, mcp: TestClient, name: str
) -> None:
    WRITERS[name](owner)
    found = next(item for item in hits(owner, "zebra") if item["kind"] == expected_kind(name))
    opened = tool(mcp, found["open_with"]["tool"], found["open_with"]["arguments"])
    fetched = owner.get(found["open_with"]["api_path"])
    assert not opened.get("isError"), opened
    assert fetched.status_code == 200, fetched.text


def test_search_channel_titles_rank_above_body_mentions(owner: TestClient) -> None:
    put_ok(owner, "episodes/1/research", {"text": "zebra " * 3})
    created(owner, "pitches", {"title": "Zebra"})
    assert next(item["kind"] for item in hits(owner, "zebra")) == "pitch"


# Pagination


def test_search_channel_pages_through_every_result_once(owner: TestClient) -> None:
    for number in range(2, 8):
        owner.post(f"{CHANNEL}/episodes", json={"title": f"Zebra {number}"})
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"q": "zebra", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = owner.get(SEARCH, params=params).json()
        assert len(page["items"]) <= 2
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == ["2", "3", "4", "5", "6", "7"]


def test_search_channel_last_page_has_no_cursor(owner: TestClient) -> None:
    write_research(owner)
    page = owner.get(SEARCH, params={"q": "zebra", "limit": 5}).json()
    assert page["next_cursor"] is None


def test_search_channel_invalid_cursor_is_a_400_problem(owner: TestClient) -> None:
    response = owner.get(SEARCH, params={"q": "zebra", "cursor": "not-a-cursor"})
    assert response.status_code == 400 and response.json()["type"].endswith("invalid-cursor")


def test_search_channel_cursor_of_another_kind_is_a_400_problem(owner: TestClient) -> None:
    episodes_cursor = owner.get(f"{CHANNEL}/episodes", params={"limit": 1})
    assert episodes_cursor.status_code == 200
    forged = "MQ"  # base64 of "1": valid base64, but not a search position
    assert owner.get(SEARCH, params={"q": "zebra", "cursor": forged}).status_code == 400


def test_search_channel_limit_above_the_maximum_is_refused(owner: TestClient) -> None:
    assert owner.get(SEARCH, params={"q": "zebra", "limit": 201}).status_code == 422


# Input safety (every query below must answer 200 and never reach FTS5 as syntax)

HOSTILE = [
    '"unbalanced',
    'say "hello" world',
    "AND",
    "zebra AND",
    "OR zebra",
    "NOT zebra",
    "NEAR(zebra giraffe)",
    "NEAR/3",
    "zebra*",
    "*",
    "(",
    "((zebra)",
    "zebra)",
    "title:zebra",
    "body : zebra",
    "^zebra",
    "zebra -giraffe",
    "{zebra}",
    "'; DROP TABLE episodes; --",
    "\\",
    "%",
]


@pytest.mark.parametrize("words", HOSTILE)
def test_search_channel_hostile_text_never_errors(owner: TestClient, words: str) -> None:
    write_research(owner)
    assert owner.get(SEARCH, params={"q": words}).status_code == 200


def test_search_channel_punctuation_only_finds_nothing(owner: TestClient) -> None:
    write_research(owner)
    assert hits(owner, '(("*"))') == []


def test_search_channel_or_is_a_word_not_an_operator(owner: TestClient) -> None:
    put_ok(owner, "episodes/1/research", {"text": "giraffe only"})
    assert hits(owner, "zebra OR giraffe") == []


def test_search_channel_near_is_a_word_not_an_operator(owner: TestClient) -> None:
    put_ok(owner, "episodes/1/research", {"text": "zebra giraffe"})
    assert hits(owner, "NEAR(zebra giraffe)") == []


def test_search_channel_column_filter_is_a_word_not_an_operator(owner: TestClient) -> None:
    write_episode_title(owner)
    assert hits(owner, "body:zebra") == []


def test_search_channel_quoted_phrase_words_are_each_required(owner: TestClient) -> None:
    put_ok(owner, "episodes/1/research", {"text": "zebra and giraffe"})
    assert kinds(owner, '"zebra giraffe"') == {"research"}


def test_match_expression_quotes_each_word_as_a_prefix() -> None:
    assert match_expression('say "hello" (world)') == '"say"* "hello"* "world"*'


def test_match_expression_without_words_is_none() -> None:
    assert match_expression('("*") --') is None


def test_match_expression_keeps_unicode_words() -> None:
    assert match_expression("produção ágil") == '"produção"* "ágil"*'


# MCP


def test_mcp_search_channel_returns_structured_hits(owner: TestClient, mcp: TestClient) -> None:
    write_research(owner)
    result = tool(mcp, "search_channel", {"slug": SLUG, "query": "zebra"})
    (found,) = result["structuredContent"]["items"]
    assert found["kind"] == "research" and found["open_with"]["tool"] == "get_research"


def test_mcp_search_channel_hostile_text_is_not_a_tool_error(
    owner: TestClient, mcp: TestClient
) -> None:
    write_research(owner)
    result = tool(mcp, "search_channel", {"slug": SLUG, "query": '"((NEAR'})
    assert not result.get("isError") and result["structuredContent"]["items"] == []


def test_mcp_search_channel_pages_with_a_cursor(owner: TestClient, mcp: TestClient) -> None:
    owner.post(f"{CHANNEL}/episodes", json={"title": "Zebra two"})
    write_research(owner)
    first = tool(mcp, "search_channel", {"slug": SLUG, "query": "zebra", "limit": 1})
    cursor = first["structuredContent"]["next_cursor"]
    second = tool(mcp, "search_channel", {"slug": SLUG, "query": "zebra", "cursor": cursor})
    assert len(first["structuredContent"]["items"]) == 1 and cursor
    assert len(second["structuredContent"]["items"]) == 1


def test_mcp_search_channel_of_another_channel_names_the_fix(
    owner: TestClient, mcp: TestClient
) -> None:
    result = tool(mcp, "search_channel", {"slug": "nowhere", "query": "zebra"})
    assert result["isError"] and "list_channels" in result["content"][0]["text"]


def test_mcp_search_channel_without_read_scope_names_the_scope(
    app: FastAPI, owner: TestClient
) -> None:
    with agent(app, new_token(owner, ["editorial:write"]), PUBLIC_URL) as client:
        result = tool(client, "search_channel", {"slug": SLUG, "query": "zebra"})
    assert result["isError"] and "channels:read" in result["content"][0]["text"]


# The migration builds the index for rows that already exist


def test_migration_indexes_rows_that_existed_before_the_index(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    old_style_database(path)
    fill(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO channel_library (id, channel_slug, kind, name, name_key, description,"
            " revision, created_at, updated_at) VALUES ('c1', 'memes', 'character', 'ZEBRA',"
            " 'zebra', 'A host', 1, '2026', '2026')"
        )
    store = ContentSearchStore(Database(path))
    found = {
        (row.kind.value, row.title)
        for word in ("pilot", "hated", "mondays", "zebra")
        if (match := match_expression(word))
        for row in store.search("memes", match, None, 10)
    }
    assert found == {
        ("episode", "Pilot"),
        ("research", "Pilot"),
        ("pitch", "Mondays"),
        ("character", "ZEBRA"),
    }


def test_migration_run_again_over_an_index_does_not_duplicate_it(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    old_style_database(path)
    fill(path)
    Database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        forget_schema_version(connection)
    store = ContentSearchStore(Database(path))
    rows = store.search("memes", '"pilot"*', None, 10)
    assert [row.kind.value for row in rows] == ["episode"]


def test_database_with_text_that_is_not_json_still_saves_a_short_script(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    old_style_database(path)
    fill(path)
    database = Database(path)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO episode_short_scripts (channel_slug, number, text, revision, updated_at)"
            " VALUES ('memes', 1, 'plain llama text', 1, '2026')"
        )
    rows = ContentSearchStore(database).search("memes", '"llama"*', None, 10)
    assert [row.kind.value for row in rows] == ["short_script"]
