"""OAuth over HTTP: discovery, registration, the consent page, the token exchange and /mcp."""

from __future__ import annotations

import json
from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor import oauth_routes
from sreditor.api import AUTH_ATTEMPTS
from sreditor.client_metadata import ClientDocuments, FetchedDocument
from sreditor.oauth import MAX_REGISTRATION_BYTES, challenge_of
from tests.conftest import RecordingMailer
from tests.workspace_helpers import ORIGIN, browser, member, submit

VERIFIER = "k" * 64
CALLBACK = "http://127.0.0.1:33418/callback"
MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


@pytest.fixture
def person(app: FastAPI, mailer: RecordingMailer) -> TestClient:
    return member(app, mailer, "ana@example.com")


@pytest.fixture
def client_id(app: FastAPI) -> str:
    response = TestClient(app).post(
        "/oauth/register", json={"redirect_uris": [CALLBACK], "client_name": "Claude"}
    )
    assert response.status_code == 201, response.text
    client: str = response.json()["client_id"]
    return client


def authorize_query(client_id: str, **changes: str) -> dict[str, str]:
    return {
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "code_challenge": challenge_of(VERIFIER),
        "code_challenge_method": "S256",
        "state": "xyz",
        "scope": "channels:read",
        **changes,
    }


def answer_of(response_location: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(response_location).query).items()}


def approve(person: TestClient, client_id: str, scopes: list[str]) -> dict[str, str]:
    response = submit(person, "/oauth/authorize", {**authorize_query(client_id), "scopes": scopes})
    assert response.status_code == 303, response.text
    return answer_of(response.headers["location"])


def exchange(app: FastAPI, client_id: str, code: str) -> dict[str, object]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "client_id": client_id,
        "code_verifier": VERIFIER,
    }
    response = TestClient(app).post("/oauth/token", data=form)
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def test_protected_resource_metadata_names_mcp_and_issuer(app: FastAPI) -> None:
    body = TestClient(app).get("/.well-known/oauth-protected-resource/mcp").json()
    assert body["resource"] == "https://sreditor.test/mcp"
    assert body["authorization_servers"] == ["https://sreditor.test"]


def test_protected_resource_metadata_at_root_matches_mcp_path(app: FastAPI) -> None:
    client = TestClient(app)
    root = client.get("/.well-known/oauth-protected-resource").json()
    assert root == client.get("/.well-known/oauth-protected-resource/mcp").json()


def test_authorization_server_metadata_requires_pkce_s256(app: FastAPI) -> None:
    body = TestClient(app).get("/.well-known/oauth-authorization-server").json()
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["registration_endpoint"] == "https://sreditor.test/oauth/register"


def test_mcp_without_token_points_to_resource_metadata(app: FastAPI) -> None:
    response = TestClient(app).post("/mcp", json=MCP_INITIALIZE, headers=MCP_HEADERS)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="https://sreditor.test/.well-known/oauth-protected-resource/mcp"'
    )


def test_register_returns_public_client(client_id: str, app: FastAPI) -> None:
    response = TestClient(app).post("/oauth/register", json={"redirect_uris": [CALLBACK]})
    assert response.json()["token_endpoint_auth_method"] == "none"


def test_register_plain_http_redirect_answers_invalid_redirect_uri(app: FastAPI) -> None:
    response = TestClient(app).post("/oauth/register", json={"redirect_uris": ["http://a.test/"]})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


def test_register_without_redirect_uris_answers_invalid_client_metadata(app: FastAPI) -> None:
    response = TestClient(app).post("/oauth/register", json={"client_name": "x"})
    assert response.json()["error"] == "invalid_client_metadata"


def test_register_unreadable_body_answers_invalid_client_metadata(app: FastAPI) -> None:
    response = TestClient(app).post("/oauth/register", content=b"{nope")
    assert response.json()["error"] == "invalid_client_metadata"


def register_with(app: FastAPI, **fields: object) -> dict[str, str]:
    response = TestClient(app).post("/oauth/register", json=fields)
    assert response.status_code == 400, response.text
    body: dict[str, str] = response.json()
    return body


def test_register_overlong_client_name_answers_invalid_client_metadata(app: FastAPI) -> None:
    refused = register_with(app, redirect_uris=[CALLBACK], client_name="n" * 201)
    assert refused["error"] == "invalid_client_metadata"


def test_register_too_many_redirect_uris_answers_invalid_client_metadata(app: FastAPI) -> None:
    uris = [f"https://claude.ai/cb/{number}" for number in range(11)]
    assert register_with(app, redirect_uris=uris)["error"] == "invalid_client_metadata"


def test_register_overlong_redirect_uri_answers_invalid_client_metadata(app: FastAPI) -> None:
    uri = "https://claude.ai/" + "x" * 2000
    assert register_with(app, redirect_uris=[uri])["error"] == "invalid_client_metadata"


def test_register_limit_sized_metadata_is_accepted(app: FastAPI) -> None:
    uris = [f"https://claude.ai/{number}/" + "x" * 1900 for number in range(10)]
    response = TestClient(app).post(
        "/oauth/register", json={"redirect_uris": uris, "client_name": "n" * 200}
    )
    assert response.status_code == 201


def test_register_body_over_the_cap_answers_413(app: FastAPI) -> None:
    padding = "x" * MAX_REGISTRATION_BYTES
    response = TestClient(app).post(
        "/oauth/register", json={"redirect_uris": [CALLBACK], "padding": padding}
    )
    assert (response.status_code, response.json()["error"]) == (413, "invalid_client_metadata")


def test_register_streamed_body_over_the_cap_answers_413(app: FastAPI) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(MAX_REGISTRATION_BYTES // 1024 + 2):
            yield b" " * 1024

    response = TestClient(app).post("/oauth/register", content=chunks())
    assert response.status_code == 413


def test_authorize_without_session_redirects_to_sign_in_and_back(
    app: FastAPI, client_id: str
) -> None:
    response = browser(app).get("/oauth/authorize", params=authorize_query(client_id))
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/oauth/authorize%3F")


def test_authorize_shows_client_name_and_requested_scope(
    person: TestClient, client_id: str
) -> None:
    response = person.get("/oauth/authorize", params=authorize_query(client_id))
    assert response.status_code == 200
    assert "Connect Claude" in response.text
    assert 'value="channels:read" checked' in response.text


def test_authorize_in_portuguese_translates_the_page(person: TestClient, client_id: str) -> None:
    query = {**authorize_query(client_id), "lang": "pt-BR"}
    assert "Conectar Claude" in person.get("/oauth/authorize", params=query).text


def test_authorize_unknown_client_shows_error_page(person: TestClient) -> None:
    response = person.get("/oauth/authorize", params=authorize_query("nope"))
    assert response.status_code == 400
    assert "This connection request is not valid" in response.text


def test_authorize_without_pkce_redirects_error_to_client(
    person: TestClient, client_id: str
) -> None:
    response = person.get(
        "/oauth/authorize", params=authorize_query(client_id, code_challenge_method="plain")
    )
    answer = answer_of(response.headers["location"])
    assert (answer["error"], answer["state"]) == ("invalid_request", "xyz")


def test_approve_redirects_to_client_with_code_and_state(
    person: TestClient, client_id: str
) -> None:
    answer = approve(person, client_id, ["channels:read"])
    assert answer["state"] == "xyz" and answer["code"]
    assert answer["iss"] == "https://sreditor.test"


def test_approve_widens_form_action_to_client_origin(person: TestClient, client_id: str) -> None:
    response = submit(
        person, "/oauth/authorize", {**authorize_query(client_id), "scopes": ["channels:read"]}
    )
    assert (
        "form-action 'self' http://127.0.0.1:33418;" in response.headers["content-security-policy"]
    )


def test_approve_from_htmx_answers_hx_redirect(person: TestClient, client_id: str) -> None:
    person.headers["hx-request"] = "true"
    response = submit(
        person, "/oauth/authorize", {**authorize_query(client_id), "scopes": ["channels:read"]}
    )
    assert response.headers["hx-redirect"].startswith(CALLBACK + "?code=")


def test_approve_without_scopes_shows_error_next_to_field(
    person: TestClient, client_id: str
) -> None:
    response = submit(person, "/oauth/authorize", authorize_query(client_id))
    assert response.status_code == 400
    assert 'id="scopes-error"' in response.text


def test_approve_forged_csrf_is_refused(person: TestClient, client_id: str) -> None:
    response = person.post(
        "/oauth/authorize", data={**authorize_query(client_id), "csrf_token": "forged"}
    )
    assert response.status_code == 403


def test_approve_tampered_redirect_shows_error_page(person: TestClient, client_id: str) -> None:
    fields = {
        **authorize_query(client_id, redirect_uri="https://evil.example/cb"),
        "scopes": ["channels:read"],
    }
    response = submit(person, "/oauth/authorize", fields)
    assert response.status_code == 400


def test_deny_redirects_access_denied_without_code(person: TestClient, client_id: str) -> None:
    response = submit(person, "/oauth/authorize/deny", authorize_query(client_id))
    answer = answer_of(response.headers["location"])
    assert answer["error"] == "access_denied" and "code" not in answer


def test_deny_unknown_client_shows_error_page(person: TestClient) -> None:
    response = submit(person, "/oauth/authorize/deny", authorize_query("nope"))
    assert response.status_code == 400


def test_deny_forged_csrf_is_refused(person: TestClient, client_id: str) -> None:
    response = person.post(
        "/oauth/authorize/deny", data={**authorize_query(client_id), "csrf_token": "forged"}
    )
    assert response.status_code == 403


def test_token_exchange_returns_bearer_api_token(
    app: FastAPI, person: TestClient, client_id: str
) -> None:
    body = exchange(app, client_id, approve(person, client_id, ["channels:read"])["code"])
    assert body["token_type"] == "Bearer" and str(body["access_token"]).startswith("sre_")
    assert body["scope"] == "channels:read"


def test_token_exchange_is_not_cached(app: FastAPI, person: TestClient, client_id: str) -> None:
    code = approve(person, client_id, ["channels:read"])["code"]
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "client_id": client_id,
        "code_verifier": VERIFIER,
    }
    assert TestClient(app).post("/oauth/token", data=form).headers["cache-control"] == "no-store"


def test_token_exchange_bad_code_answers_invalid_grant(app: FastAPI, client_id: str) -> None:
    response = TestClient(app).post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": "nope", "client_id": client_id},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_oauth_token_lists_on_tokens_page(app: FastAPI, person: TestClient, client_id: str) -> None:
    exchange(app, client_id, approve(person, client_id, ["channels:read"])["code"])
    assert "Claude (OAuth)" in person.get("/app/account/tokens").text


def test_oauth_token_opens_mcp_session(app: FastAPI, person: TestClient, client_id: str) -> None:
    body = exchange(app, client_id, approve(person, client_id, ["channels:read"])["code"])
    with TestClient(app, base_url=ORIGIN) as agent:
        response = agent.post(
            "/mcp",
            json=MCP_INITIALIZE,
            headers={**MCP_HEADERS, "authorization": f"Bearer {body['access_token']}"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["serverInfo"]["name"]


CIMD_URL = "https://claude.ai/oauth/mcp-oauth-client-metadata"


@pytest.fixture
def cimd_client(monkeypatch: pytest.MonkeyPatch) -> str:
    """A client that names itself by a metadata document URL, served without a network."""
    body = json.dumps(
        {"client_id": CIMD_URL, "client_name": "Claude CIMD", "redirect_uris": [CALLBACK]}
    ).encode()
    documents = ClientDocuments(fetcher=lambda url: FetchedDocument(body=body, cache_seconds=60))
    monkeypatch.setattr(oauth_routes, "CLIENT_DOCUMENTS", documents)
    return CIMD_URL


def test_authorization_server_metadata_advertises_cimd_and_iss(app: FastAPI) -> None:
    body = TestClient(app).get("/.well-known/oauth-authorization-server").json()
    assert body["client_id_metadata_document_supported"] is True
    assert body["authorization_response_iss_parameter_supported"] is True


def test_authorize_cimd_client_shows_its_document_name(
    person: TestClient, cimd_client: str
) -> None:
    response = person.get("/oauth/authorize", params=authorize_query(cimd_client))
    assert "Connect Claude CIMD" in response.text


def test_cimd_client_code_exchanges_for_token(
    app: FastAPI, person: TestClient, cimd_client: str
) -> None:
    body = exchange(app, cimd_client, approve(person, cimd_client, ["channels:read"])["code"])
    assert str(body["access_token"]).startswith("sre_")


def test_authorize_cimd_document_that_fails_shows_error_page(
    person: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = ClientDocuments(fetcher=lambda url: FetchedDocument(body=b"{}", cache_seconds=0))
    monkeypatch.setattr(oauth_routes, "CLIENT_DOCUMENTS", broken)
    response = person.get("/oauth/authorize", params=authorize_query(CIMD_URL))
    assert response.status_code == 400


def test_authorize_over_the_rate_limit_shows_error_page_with_retry_after(
    person: TestClient, client_id: str
) -> None:
    for _ in range(AUTH_ATTEMPTS):
        person.get("/oauth/authorize", params=authorize_query(client_id))
    response = person.get("/oauth/authorize", params=authorize_query(client_id))
    assert response.status_code == 429 and int(response.headers["retry-after"]) >= 1
    assert "Too many attempts" in response.text


def test_authorize_over_the_rate_limit_in_portuguese_translates_the_page(
    person: TestClient, client_id: str
) -> None:
    query = {**authorize_query(client_id), "lang": "pt-BR"}
    for _ in range(AUTH_ATTEMPTS):
        person.get("/oauth/authorize", params=query)
    assert "Tentativas demais" in person.get("/oauth/authorize", params=query).text


def test_authorize_after_a_limited_client_other_clients_are_not_limited(
    app: FastAPI, person: TestClient, client_id: str
) -> None:
    for _ in range(AUTH_ATTEMPTS + 1):
        person.get("/oauth/authorize", params=authorize_query(client_id))
    assert person.get("/oauth/authorize/deny").status_code != 429


def test_approve_over_the_rate_limit_is_refused(person: TestClient, client_id: str) -> None:
    for _ in range(AUTH_ATTEMPTS):
        person.post("/oauth/authorize", data=authorize_query(client_id))
    assert person.post("/oauth/authorize", data=authorize_query(client_id)).status_code == 429


def test_deny_over_the_rate_limit_is_refused(person: TestClient, client_id: str) -> None:
    for _ in range(AUTH_ATTEMPTS):
        person.post("/oauth/authorize/deny", data=authorize_query(client_id))
    assert person.post("/oauth/authorize/deny", data=authorize_query(client_id)).status_code == 429
