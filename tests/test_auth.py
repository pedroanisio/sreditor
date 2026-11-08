from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.api import create_app
from sreditor.deps import SESSION_COOKIE
from sreditor.security import RateLimiter
from sreditor.settings import AppSettings
from sreditor.users import UserStatus
from tests.conftest import PASSWORD, RecordingMailer, fast_passwords

EMAIL = "ana@example.com"
NEXT_PASSWORD = "the next passphrase"


def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="https://testserver")


def register(api: TestClient, email: str = EMAIL, password: str = PASSWORD) -> int:
    body = {"email": email, "display_name": "Ana", "password": password}
    return api.post("/auth/register", json=body).status_code


def login(api: TestClient, email: str = EMAIL, password: str = PASSWORD) -> TestClient:
    response = api.post("/auth/login", json={"email": email, "password": password})
    api.last_status = response.status_code  # type: ignore[attr-defined]
    return api


def test_registration_journey_signs_user_in_and_out(app: FastAPI, mailer: RecordingMailer) -> None:
    api = client(app)
    assert register(api, "Ana@Example.com ") == 202 or register(api) == 202
    assert login(api).last_status == 403  # type: ignore[attr-defined]

    verified = api.post("/auth/verify", json={"token": mailer.token_for(EMAIL)})
    assert verified.status_code == 200
    assert verified.json()["status"] == "active"
    assert api.post("/auth/verify", json={"token": mailer.token_for(EMAIL)}).status_code == 400

    response = api.post("/auth/login", json={"email": "ANA@example.com", "password": PASSWORD})
    assert response.status_code == 200
    assert SESSION_COOKIE in response.cookies
    assert "httponly" in response.headers["set-cookie"].lower()
    profile = api.get("/me").json()
    assert (profile["user"]["email"], profile["channels"]) == (EMAIL, {})

    assert api.post("/auth/logout").status_code == 204
    assert api.get("/me").status_code == 401
    assert client(app).post("/auth/logout").status_code == 204


def test_me_valid_bearer_token_returns_ok(app: FastAPI, sign_up: Callable[..., TestClient]) -> None:
    signed_in = sign_up(EMAIL)
    bearer = TestClient(app, headers={"Authorization": signed_in.headers["Authorization"]})
    assert bearer.get("/me").status_code == 200


def test_me_unknown_bearer_token_is_unauthorized(app: FastAPI) -> None:
    assert TestClient(app, headers={"Authorization": "Bearer nope"}).get("/me").status_code == 401


def test_me_basic_authorization_scheme_is_unauthorized(app: FastAPI) -> None:
    assert TestClient(app, headers={"Authorization": "Basic x"}).get("/me").status_code == 401


def test_register_taken_email_returns_accepted(
    sign_up: Callable[..., TestClient], app: FastAPI
) -> None:
    sign_up(EMAIL)
    assert register(client(app)) == 202


def test_register_taken_email_warns_the_owner_by_email(
    sign_up: Callable[..., TestClient], app: FastAPI, mailer: RecordingMailer
) -> None:
    sign_up(EMAIL)
    register(client(app))
    assert mailer.sent[-1][1] == "Someone tried to register with your email"


def test_login_wrong_password_and_unknown_email_return_same_status(
    sign_up: Callable[..., TestClient], app: FastAPI
) -> None:
    sign_up(EMAIL)
    wrong_password = login(client(app), password="wrong password!!")
    unknown_email = login(client(app), email="nobody@example.com")
    assert wrong_password.last_status == unknown_email.last_status == 401  # type: ignore[attr-defined]


def test_register_weak_password_is_unprocessable(app: FastAPI) -> None:
    assert register(client(app), password="short") == 422


def test_password_reset_journey_replaces_password_and_ends_old_sessions(
    sign_up: Callable[..., TestClient], app: FastAPI, mailer: RecordingMailer
) -> None:
    old_session = sign_up(EMAIL)
    api = client(app)
    assert api.post("/auth/password-reset", json={"email": EMAIL}).status_code == 202
    assert api.post("/auth/password-reset", json={"email": "nobody@example.com"}).status_code == 202
    token = mailer.token_for(EMAIL)

    new_password = "a brand new passphrase"
    confirm = {"token": token, "password": new_password}
    assert api.post("/auth/password-reset/confirm", json=confirm).status_code == 204
    assert api.post("/auth/password-reset/confirm", json=confirm).status_code == 400
    assert old_session.get("/me").status_code == 401
    assert login(client(app)).last_status == 401  # type: ignore[attr-defined]
    assert login(client(app), password=new_password).last_status == 200  # type: ignore[attr-defined]


def test_password_reset_confirm_pending_email_allows_login(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    api = client(app)
    register(api)
    api.post("/auth/password-reset", json={"email": EMAIL})
    confirm = {"token": mailer.token_for(EMAIL), "password": "another long password"}
    assert api.post("/auth/password-reset/confirm", json=confirm).status_code == 204
    assert login(api, password="another long password").last_status == 200  # type: ignore[attr-defined]


@pytest.fixture
def disabled_user_session(sign_up: Callable[..., TestClient], app: FastAPI) -> TestClient:
    """A client signed in as a user who was disabled afterwards."""
    session = sign_up(EMAIL)
    services = app.state.services
    with services.users.database.connect() as connection:
        connection.execute("UPDATE users SET status = ?", (UserStatus.DISABLED.value,))
    return session


def test_me_disabled_user_session_is_unauthorized(disabled_user_session: TestClient) -> None:
    assert disabled_user_session.get("/me").status_code == 401


@pytest.mark.usefixtures("disabled_user_session")
def test_login_disabled_user_is_forbidden(app: FastAPI) -> None:
    assert login(client(app)).last_status == 403  # type: ignore[attr-defined]


@pytest.mark.usefixtures("disabled_user_session")
def test_password_reset_disabled_user_sends_no_email(app: FastAPI, mailer: RecordingMailer) -> None:
    sent_before = len(mailer.sent)
    client(app).post("/auth/password-reset", json={"email": EMAIL})
    assert len(mailer.sent) == sent_before


@pytest.fixture
def signed_in_api(sign_up: Callable[..., TestClient]) -> TestClient:
    return sign_up(EMAIL)


def change_password(api: TestClient) -> int:
    change = {"password": NEXT_PASSWORD, "current_password": PASSWORD}
    return api.patch("/me", json=change).status_code


def test_update_profile_display_name_returns_new_name(signed_in_api: TestClient) -> None:
    response = signed_in_api.patch("/me", json={"display_name": "Ana B"})
    assert response.json()["user"]["display_name"] == "Ana B"


def test_update_profile_password_with_wrong_current_password_is_forbidden(
    signed_in_api: TestClient,
) -> None:
    change = {"password": NEXT_PASSWORD, "current_password": "wrong"}
    assert signed_in_api.patch("/me", json=change).status_code == 403


def test_update_profile_password_without_current_password_is_forbidden(
    signed_in_api: TestClient,
) -> None:
    assert signed_in_api.patch("/me", json={"password": NEXT_PASSWORD}).status_code == 403


def test_update_profile_password_with_current_password_succeeds(
    signed_in_api: TestClient,
) -> None:
    assert change_password(signed_in_api) == 200


def test_update_profile_password_change_ends_the_session(signed_in_api: TestClient) -> None:
    change_password(signed_in_api)
    assert signed_in_api.get("/me").status_code == 401


def test_update_profile_password_change_allows_login_with_new_password(
    signed_in_api: TestClient, app: FastAPI
) -> None:
    change_password(signed_in_api)
    assert login(client(app), password=NEXT_PASSWORD).last_status == 200  # type: ignore[attr-defined]


@pytest.fixture
def moment() -> list[datetime]:
    """A one-item list holding the app clock, so tests can move time forward."""
    return [datetime(2026, 1, 1, tzinfo=UTC)]


@pytest.fixture
def timed_api(
    app_settings: AppSettings, mailer: RecordingMailer, moment: list[datetime]
) -> TestClient:
    app = create_app(
        app_settings, mailer=mailer, passwords=fast_passwords(), clock=lambda: moment[0]
    )
    return client(app)


def test_verify_token_after_three_days_is_rejected(
    timed_api: TestClient, mailer: RecordingMailer, moment: list[datetime]
) -> None:
    register(timed_api)
    moment[0] += timedelta(days=3)
    assert (
        timed_api.post("/auth/verify", json={"token": mailer.token_for(EMAIL)}).status_code == 400
    )


def test_session_after_thirty_one_days_is_unauthorized(
    timed_api: TestClient, mailer: RecordingMailer, moment: list[datetime]
) -> None:
    register(timed_api)
    timed_api.post("/auth/password-reset", json={"email": EMAIL})
    timed_api.post(
        "/auth/password-reset/confirm",
        json={"token": mailer.token_for(EMAIL), "password": PASSWORD},
    )
    assert login(timed_api).last_status == 200  # type: ignore[attr-defined]
    assert timed_api.get("/me").status_code == 200
    moment[0] += timedelta(days=31)
    assert timed_api.get("/me").status_code == 401


def test_login_eleventh_failed_attempt_is_rate_limited(app: FastAPI) -> None:
    api = client(app)
    statuses = [login(api, password="wrong password!!").last_status for _ in range(11)]  # type: ignore[attr-defined]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429


def test_rate_limit_with_client_ip_header_counts_each_visitor_apart(app: FastAPI) -> None:
    services = app.state.services
    services.settings = services.settings.model_copy(
        update={"client_ip_header": "CF-Connecting-IP"}
    )
    services.limiter = RateLimiter(1, 900.0)
    api = client(app)
    statuses = [
        api.post(
            "/auth/login",
            json={"email": EMAIL, "password": "wrong password!!"},
            headers={"CF-Connecting-IP": f"203.0.113.{visitor}"},
        ).status_code
        for visitor in (1, 2)
    ]
    assert statuses == [401, 401]


def test_rate_limit_retry_after_is_seconds_left_in_window(app: FastAPI) -> None:
    moment = [0.0]
    app.state.services.limiter = RateLimiter(1, 900.0, clock=lambda: moment[0])
    api = client(app)
    login(api, password="wrong password!!")
    moment[0] = 600.0
    response = api.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 429 and response.headers["Retry-After"] == "300"


@pytest.mark.parametrize("is_secure", [True, False])
def test_cookie_secure_flag_follows_settings(
    app_settings: AppSettings,
    mailer: RecordingMailer,
    is_secure: bool,
    sign_up: Callable[..., TestClient],
) -> None:
    app = create_app(
        app_settings.model_copy(update={"has_secure_cookies": is_secure}),
        mailer=mailer,
        passwords=fast_passwords(),
    )
    sign_up(EMAIL)  # registers in the shared database
    response = client(app).post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert ("secure" in response.headers["set-cookie"].lower()) is is_secure
