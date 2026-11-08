"""The OAuth service: client rules, PKCE, single-use codes and the token they become."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from sreditor.database import OAUTH_CLIENT_USE, Database
from sreditor.oauth import (
    CODE_TTL,
    DEFAULT_SCOPES,
    MAX_CLIENT_NAME_LENGTH,
    MAX_REDIRECT_URI_LENGTH,
    MAX_REDIRECT_URIS,
    UNUSED_CLIENT_TTL,
    AuthorizationRequest,
    ClientRegistration,
    ErrorCode,
    Grant,
    OAuthError,
    OAuthStore,
    TokenRequest,
    challenge_of,
    is_allowed_redirect,
    is_registered_redirect,
    redirect_with,
    requested_scopes,
    resources_for,
    verifier_matches,
)
from sreditor.tokens import Scope, TokenStore
from sreditor.users import UserStore
from tests.conftest import fast_passwords

VERIFIER = "v" * 43
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
RESOURCES = resources_for("https://sreditor.test", "/mcp")


class Clock:
    """A clock the test moves forward."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 6, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def database(tmp_path: Path, clock: Clock) -> Database:
    return Database(tmp_path / "oauth.sqlite", clock)


@pytest.fixture
def user_id(database: Database) -> UUID:
    users = UserStore(database)
    user = users.create("ana@example.com", "Ana", fast_passwords().hash("a long passphrase"))
    users.activate(user.id)
    return user.id


@pytest.fixture
def store(database: Database) -> OAuthStore:
    return OAuthStore(database, TokenStore(database, UserStore(database)))


def registered(store: OAuthStore) -> str:
    return store.register(
        ClientRegistration(redirect_uris=[CALLBACK], client_name="Claude")
    ).client_id


def authorization(client_id: str, **changes: str) -> AuthorizationRequest:
    fields = {
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "code_challenge": challenge_of(VERIFIER),
        "code_challenge_method": "S256",
        **changes,
    }
    return AuthorizationRequest(**fields)


def issued_code(store: OAuthStore, user_id: UUID, client_id: str) -> str:
    grant = Grant(
        client_id=client_id,
        user_id=user_id,
        redirect_uri=CALLBACK,
        code_challenge=challenge_of(VERIFIER),
        scopes=[Scope.CHANNELS_READ],
    )
    return store.issue_code(grant)


def token_request(client_id: str, code: str, **changes: str) -> TokenRequest:
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CALLBACK,
        "client_id": client_id,
        "code_verifier": VERIFIER,
        **changes,
    }
    return TokenRequest(**fields)


def refusal_code(action: object) -> ErrorCode:
    with pytest.raises(OAuthError) as raised:
        action()  # type: ignore[operator]
    return raised.value.code


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/cb",
        "http://localhost:3334/callback",
        "http://127.0.0.1/cb",
        "http://[::1]:9/cb",
    ],
)
def test_is_allowed_redirect_https_or_loopback_is_allowed(uri: str) -> None:
    assert is_allowed_redirect(uri)


@pytest.mark.parametrize(
    "uri",
    ["http://evil.example/cb", "https://claude.ai/cb#frag", "javascript:alert(1)", "https:///x"],
)
def test_is_allowed_redirect_other_uris_are_refused(uri: str) -> None:
    assert not is_allowed_redirect(uri)


def test_client_registration_with_plain_http_redirect_is_rejected() -> None:
    with pytest.raises(ValidationError, match="https or loopback"):
        ClientRegistration(redirect_uris=["http://evil.example/cb"])


def test_client_registration_with_client_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Only public clients"):
        ClientRegistration(
            redirect_uris=[CALLBACK], token_endpoint_auth_method="client_secret_post"
        )


def test_register_then_client_returns_same_client(store: OAuthStore) -> None:
    client_id = registered(store)
    client = store.client(client_id)
    assert client is not None and client.redirect_uris == [CALLBACK]


def test_client_unknown_is_none(store: OAuthStore) -> None:
    assert store.client("nope") is None


def test_challenge_of_matches_rfc7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert challenge_of(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_matches_short_verifier_is_refused() -> None:
    assert not verifier_matches("short", challenge_of("short"))


def test_requested_scopes_unknown_only_gives_defaults() -> None:
    assert requested_scopes("openid profile") == list(DEFAULT_SCOPES)


def test_requested_scopes_known_are_kept() -> None:
    assert requested_scopes("channels:read editorial:write") == [
        Scope.CHANNELS_READ,
        Scope.EDITORIAL_WRITE,
    ]


def test_redirect_with_existing_query_appends_parameters() -> None:
    assert redirect_with("https://a.test/cb?x=1", {"code": "c", "state": ""}) == (
        "https://a.test/cb?x=1&code=c"
    )


def test_check_valid_request_returns_client(store: OAuthStore) -> None:
    client_id = registered(store)
    assert store.check(authorization(client_id), RESOURCES).client_name == "Claude"


def test_check_unknown_client_is_not_redirectable(store: OAuthStore) -> None:
    with pytest.raises(OAuthError) as raised:
        store.check(authorization("nope"), RESOURCES)
    assert not raised.value.can_redirect


def test_check_unregistered_redirect_is_refused(store: OAuthStore) -> None:
    client_id = registered(store)
    request = authorization(client_id, redirect_uri="https://evil.example/cb")
    assert refusal_code(lambda: store.check(request, RESOURCES)) is ErrorCode.INVALID_REQUEST


LOOPBACK_CALLBACK = "http://localhost/callback"


@pytest.mark.parametrize(
    "uri",
    ["http://localhost:63054/callback", "http://localhost/callback", "http://localhost:1/callback"],
)
def test_is_registered_redirect_loopback_any_port_matches(uri: str) -> None:
    assert is_registered_redirect(uri, [LOOPBACK_CALLBACK])


def test_is_registered_redirect_ipv6_loopback_any_port_matches() -> None:
    assert is_registered_redirect("http://[::1]:8123/callback", ["http://[::1]/callback"])


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:63054/other",
        "http://127.0.0.1:63054/callback",
        "http://localhost:63054/callback?next=x",
        "https://localhost:63054/callback",
        "http://localhost@evil.example:63054/callback",
    ],
)
def test_is_registered_redirect_loopback_other_path_host_or_scheme_is_refused(uri: str) -> None:
    assert not is_registered_redirect(uri, [LOOPBACK_CALLBACK])


def test_is_registered_redirect_https_other_port_is_refused() -> None:
    assert not is_registered_redirect("https://claude.ai:8443/api/mcp/auth_callback", [CALLBACK])


def test_check_loopback_redirect_on_another_port_is_accepted(store: OAuthStore) -> None:
    client_id = store.register(ClientRegistration(redirect_uris=[LOOPBACK_CALLBACK])).client_id
    request = authorization(client_id, redirect_uri="http://localhost:63054/callback")
    assert store.check(request, RESOURCES).client_id == client_id


def test_check_without_pkce_is_refused(store: OAuthStore) -> None:
    client_id = registered(store)
    request = authorization(client_id, code_challenge_method="plain")
    assert refusal_code(lambda: store.check(request, RESOURCES)) is ErrorCode.INVALID_REQUEST


def test_check_token_response_type_is_refused(store: OAuthStore) -> None:
    client_id = registered(store)
    request = authorization(client_id, response_type="token")
    assert (
        refusal_code(lambda: store.check(request, RESOURCES)) is ErrorCode.UNSUPPORTED_RESPONSE_TYPE
    )


def test_check_other_resource_is_refused(store: OAuthStore) -> None:
    client_id = registered(store)
    request = authorization(client_id, resource="https://other.test/mcp")
    assert refusal_code(lambda: store.check(request, RESOURCES)) is ErrorCode.INVALID_TARGET


def test_check_this_mcp_resource_is_accepted(store: OAuthStore) -> None:
    client_id = registered(store)
    store.check(authorization(client_id, resource="https://sreditor.test/mcp"), RESOURCES)


def test_exchange_valid_code_issues_api_token_with_granted_scopes(
    store: OAuthStore, user_id: UUID
) -> None:
    client_id = registered(store)
    issued = store.exchange(token_request(client_id, issued_code(store, user_id, client_id)))
    assert issued.secret.startswith("sre_")
    assert issued.token.scopes == [Scope.CHANNELS_READ]


def test_exchange_names_token_after_client(store: OAuthStore, user_id: UUID) -> None:
    client_id = registered(store)
    issued = store.exchange(token_request(client_id, issued_code(store, user_id, client_id)))
    assert issued.token.name == "Claude (OAuth)"


def test_exchange_code_twice_is_refused(store: OAuthStore, user_id: UUID) -> None:
    client_id = registered(store)
    request = token_request(client_id, issued_code(store, user_id, client_id))
    store.exchange(request)
    assert refusal_code(lambda: store.exchange(request)) is ErrorCode.INVALID_GRANT


def test_exchange_expired_code_is_refused(store: OAuthStore, user_id: UUID, clock: Clock) -> None:
    client_id = registered(store)
    request = token_request(client_id, issued_code(store, user_id, client_id))
    clock.now += timedelta(minutes=11)
    assert refusal_code(lambda: store.exchange(request)) is ErrorCode.INVALID_GRANT


def test_exchange_wrong_verifier_is_refused(store: OAuthStore, user_id: UUID) -> None:
    client_id = registered(store)
    code = issued_code(store, user_id, client_id)
    request = token_request(client_id, code, code_verifier="w" * 43)
    assert refusal_code(lambda: store.exchange(request)) is ErrorCode.INVALID_GRANT


def test_exchange_other_client_is_refused(store: OAuthStore, user_id: UUID) -> None:
    client_id = registered(store)
    request = token_request(registered(store), issued_code(store, user_id, client_id))
    assert refusal_code(lambda: store.exchange(request)) is ErrorCode.INVALID_GRANT


def test_exchange_other_redirect_is_refused(store: OAuthStore, user_id: UUID) -> None:
    client_id = registered(store)
    code = issued_code(store, user_id, client_id)
    request = token_request(client_id, code, redirect_uri="https://claude.ai/other")
    assert refusal_code(lambda: store.exchange(request)) is ErrorCode.INVALID_GRANT


def test_exchange_other_grant_type_is_refused(store: OAuthStore) -> None:
    request = token_request("c", "x", grant_type="client_credentials")
    assert refusal_code(lambda: store.exchange(request)) is ErrorCode.UNSUPPORTED_GRANT_TYPE


def count_rows(database: Database, table: str) -> int:
    with database.connect() as connection:
        total: int = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return total


def test_client_registration_with_overlong_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ClientRegistration(redirect_uris=[CALLBACK], client_name="n" * (MAX_CLIENT_NAME_LENGTH + 1))


def test_client_registration_with_too_many_redirects_is_rejected() -> None:
    uris = [f"{CALLBACK}/{number}" for number in range(MAX_REDIRECT_URIS + 1)]
    with pytest.raises(ValidationError):
        ClientRegistration(redirect_uris=uris)


def test_client_registration_with_overlong_redirect_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ClientRegistration(redirect_uris=[CALLBACK + "/" + "x" * MAX_REDIRECT_URI_LENGTH])


def test_register_unused_client_past_the_cutoff_is_purged(store: OAuthStore, clock: Clock) -> None:
    stale = registered(store)
    clock.now += UNUSED_CLIENT_TTL + timedelta(days=1)
    registered(store)
    assert store.client(stale) is None


def test_register_unused_client_within_the_cutoff_is_kept(store: OAuthStore, clock: Clock) -> None:
    young = registered(store)
    clock.now += UNUSED_CLIENT_TTL - timedelta(days=1)
    registered(store)
    assert store.client(young) is not None


def test_register_client_that_was_approved_is_never_purged(
    store: OAuthStore, user_id: UUID, clock: Clock
) -> None:
    used = registered(store)
    issued_code(store, user_id, used)
    clock.now += UNUSED_CLIENT_TTL * 3
    registered(store)
    assert store.client(used) is not None


def test_register_client_with_a_live_token_is_never_purged(
    store: OAuthStore, user_id: UUID, clock: Clock
) -> None:
    holder = registered(store)
    store.exchange(token_request(holder, issued_code(store, user_id, holder)))
    clock.now += UNUSED_CLIENT_TTL + timedelta(days=1)
    registered(store)
    assert store.client(holder) is not None


def test_register_expired_code_is_purged(
    store: OAuthStore, database: Database, user_id: UUID, clock: Clock
) -> None:
    issued_code(store, user_id, registered(store))
    clock.now += CODE_TTL + timedelta(seconds=1)
    registered(store)
    assert count_rows(database, "oauth_codes") == 0


def test_register_pending_code_is_kept(
    store: OAuthStore, database: Database, user_id: UUID, clock: Clock
) -> None:
    issued_code(store, user_id, registered(store))
    clock.now += CODE_TTL - timedelta(seconds=1)
    registered(store)
    assert count_rows(database, "oauth_codes") == 1


def test_issue_code_purges_expired_codes(
    store: OAuthStore, database: Database, user_id: UUID, clock: Clock
) -> None:
    client_id = registered(store)
    issued_code(store, user_id, client_id)
    clock.now += CODE_TTL + timedelta(seconds=1)
    issued_code(store, user_id, client_id)
    assert count_rows(database, "oauth_codes") == 1


def test_exchange_leaves_no_used_code_behind(
    store: OAuthStore, database: Database, user_id: UUID
) -> None:
    client_id = registered(store)
    store.exchange(token_request(client_id, issued_code(store, user_id, client_id)))
    assert count_rows(database, "oauth_codes") == 0


def last_used(database: Database, client_id: str) -> str | None:
    with database.connect() as connection:
        value: str | None = connection.execute(
            "SELECT last_used_at FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()[0]
    return value


def test_migrate_client_with_a_token_counts_as_used(
    store: OAuthStore, database: Database, user_id: UUID, clock: Clock
) -> None:
    holder = registered(store)
    store.exchange(token_request(holder, issued_code(store, user_id, holder)))
    idle = store.register(
        ClientRegistration(redirect_uris=[CALLBACK], client_name="Idle")
    ).client_id
    with database.connect() as connection:
        connection.execute("DROP INDEX oauth_clients_unused")
        connection.execute("DROP INDEX oauth_codes_by_expiry")
        connection.execute(
            "DELETE FROM schema_version WHERE version = ?", (OAUTH_CLIENT_USE.version,)
        )
        connection.execute("ALTER TABLE oauth_clients DROP COLUMN last_used_at")
    reopened = Database(database.path, clock)
    assert (last_used(reopened, holder) is not None, last_used(reopened, idle)) == (True, None)
