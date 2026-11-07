from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import BaseModel, ValidationError, model_validator

from sreditor.deps import SESSION_COOKIE
from sreditor.i18n import Translator
from sreditor.store import StoreSettings
from sreditor.web.forms import CSRF_COOKIE, field_errors, message_for
from sreditor.web.routes import APP_PATH, SECURITY_HEADERS, safe_next
from tests.conftest import PASSWORD, RecordingMailer

ORIGIN = "https://sreditor.test"  # the public_url in conftest's app_settings
EMAIL = "ana@example.com"
CHANNEL = {"slug": "funny-memes", "name": "Funny Memes"}


def browser(app: FastAPI) -> TestClient:
    """A client that behaves like a browser on our site: same origin, no automatic redirects."""
    return TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN}, follow_redirects=False)


def submit(client: TestClient, path: str, data: dict[str, str], **headers: str) -> Response:
    """Open the page to get a CSRF token, as a browser would, then post the form."""
    if CSRF_COOKIE not in client.cookies:
        client.get("/login")
    token = client.cookies[CSRF_COOKIE]
    return client.post(path, data={"csrf_token": token, **data}, headers=headers)


def register(client: TestClient, email: str = EMAIL, **fields: str) -> Response:
    form = {"email": email, "display_name": "Ana", "password": PASSWORD, "consent": "on"}
    return submit(client, "/register", {**form, **fields})


def link(mailer: RecordingMailer, to: str) -> str:
    """The path of the link in the latest email to an address."""
    body = next(body for address, _, body in reversed(mailer.sent) if address == to)
    url = next(word for word in body.split() if word.startswith(ORIGIN))
    return url.removeprefix(ORIGIN)


def token_in(path: str) -> str:
    """The token of an email link's path, from its #token= fragment."""
    return path.split("#token=")[1]


def open_invitation(client: TestClient, path: str) -> Response:
    """Do what token.js does on /invitations: post the fragment's token to open it."""
    return submit(client, "/invitations/open", {"token": token_in(path)})


def signed_up(app: FastAPI, mailer: RecordingMailer, email: str = EMAIL) -> TestClient:
    """A browser that registered, verified and signed in through the pages."""
    client = browser(app)
    assert register(client, email).status_code == 303
    token = token_in(link(mailer, email))
    assert submit(client, "/verify", {"token": token}).status_code == 200
    response = submit(client, "/login", {"email": email, "password": PASSWORD})
    assert response.status_code == 303
    return client


def test_landing_page_for_visitor_shows_platforms_and_roles(app: FastAPI) -> None:
    response = browser(app).get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Run every channel from one place." in response.text
    for name in ("TikTok", "YouTube", "Instagram", "X", "Facebook", "Kwai"):
        assert f'<li><span class="tag outline">{name}</span></li>' in response.text
    assert "owner, editor, contributor or viewer" in response.text


def test_landing_page_response_sends_the_security_headers(app: FastAPI) -> None:
    response = browser(app).get("/")
    for header, value in SECURITY_HEADERS.items():
        assert response.headers[header] == value


def test_landing_page_response_sets_a_strict_http_only_csrf_cookie(app: FastAPI) -> None:
    cookie = browser(app).get("/").headers["set-cookie"].lower()
    assert CSRF_COOKIE in cookie and "httponly" in cookie and "samesite=strict" in cookie


def test_referrer_policy_for_plain_form_posts_is_same_origin(app: FastAPI) -> None:
    # "no-referrer" makes Chrome send Origin: null on plain form posts, failing every CSRF check.
    policy = browser(app).get("/").headers["referrer-policy"]
    assert policy == "same-origin"


def test_assets_on_public_pages_are_served_locally(app: FastAPI) -> None:
    client = browser(app)
    page = client.get("/").text
    assert 'src="https://sreditor.test/static/htmx.min.js"' in page
    assert "cdn" not in page.lower() and "unpkg" not in page
    for asset in ("htmx.min.js", "style.css", "icon.svg"):
        assert client.get(f"/static/{asset}").status_code == 200


def test_openapi_schema_with_pages_lists_only_the_api(app: FastAPI) -> None:
    paths = browser(app).get("/openapi.json").json()["paths"]
    assert "/register" not in paths and "/auth/register" in paths


def test_registration_from_form_to_verified_email_allows_sign_in(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    client = browser(app)
    assert client.get("/register").status_code == 200

    response = register(client)
    assert (response.status_code, response.headers["location"]) == (303, "/register/sent")
    assert "Check your email" in client.get("/register/sent").text

    # The token travels in the fragment, which browsers never send (web.email.links).
    verify_path = link(mailer, EMAIL)
    assert verify_path.startswith("/verify#token=")
    token = token_in(verify_path)
    opened = client.get("/verify")
    assert opened.status_code == 200 and "data-token-form" in opened.text
    assert token not in opened.text

    # Opening a link only shows a form; it does not spend the token (web.tokens.post-only).
    for _ in range(2):
        legacy = client.get(f"/verify?token={token}")
        assert legacy.status_code == 200 and f'value="{token}"' in legacy.text
    pending = submit(client, "/login", {"email": EMAIL, "password": PASSWORD})
    assert pending.status_code == 403 and "Verify your email first" in pending.text

    assert "Email verified" in submit(client, "/verify", {"token": token}).text
    again = submit(client, "/verify", {"token": token})
    assert again.status_code == 400
    assert 'id="token-error">This link is invalid or has expired.' in again.text


def test_verify_with_pasted_link_verifies_the_email(app: FastAPI, mailer: RecordingMailer) -> None:
    client = browser(app)
    register(client)
    pasted = f"  {ORIGIN}{link(mailer, EMAIL)}  "
    assert "Email verified" in submit(client, "/verify", {"token": pasted}).text


def test_registration_with_invalid_fields_shows_errors_next_to_them(app: FastAPI) -> None:
    client = browser(app)
    response = register(client, "not-an-email", password="short", consent="", display_name="")

    assert response.status_code == 422
    assert 'id="email-error">Enter a valid email address.' in response.text
    assert 'id="password-error">Use at least 12 characters.' in response.text
    assert 'id="display_name-error">This field is required.' in response.text
    assert 'id="consent-error">Accept the terms of service' in response.text
    assert 'value="not-an-email"' in response.text
    assert "short" not in response.text.split("<form")[1]  # the password is never echoed

    too_long = register(client, "b@example.com", display_name="x" * 81)
    assert "Use at most 80 characters." in too_long.text


def test_registration_with_taken_email_shows_the_same_page(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    signed_up(app, mailer)
    response = register(browser(app))

    assert (response.status_code, response.headers["location"]) == (303, "/register/sent")
    to, subject, body = mailer.sent[-1]
    assert (to, subject) == (EMAIL, "Someone tried to register with your email")
    assert f"{ORIGIN}/login" in body and f"{ORIGIN}/forgot-password" in body


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"csrf_token": "forged"}, "token does not match the cookie"),
        ({"origin": "https://evil.example"}, "another origin"),
        ({"origin": None}, "no origin"),
        ({"cookie": None}, "no CSRF cookie"),
    ],
)
def test_forms_with_foreign_origin_or_bad_csrf_are_refused(
    app: FastAPI, change: dict[str, Any], reason: str
) -> None:
    client = browser(app)
    client.get("/register")
    token = client.cookies[CSRF_COOKIE]
    if "origin" in change:
        if change["origin"] is None:
            del client.headers["origin"]
        else:
            client.headers["origin"] = change["origin"]
    if "cookie" in change:
        client.cookies.clear()
    data = {"csrf_token": change.get("csrf_token", token), "email": EMAIL, "password": PASSWORD}

    paths = ("/register", "/login", "/logout", "/verify", "/forgot-password", "/reset-password")
    for path in (*paths, "/invitations/open", "/invitations/join"):
        response = client.post(path, data=data)
        assert response.status_code == 403, (path, reason)
        assert "This form has expired" in response.text


def test_sign_in_then_out_ends_the_session(app: FastAPI, mailer: RecordingMailer) -> None:
    client = signed_up(app, mailer)
    assert SESSION_COOKIE in client.cookies

    home = client.get(APP_PATH)
    assert "<span>Ana</span>" in home.text and "No channels yet" in home.text
    assert client.get("/").headers["location"] == APP_PATH
    assert client.get("/login?next=/terms").headers["location"] == "/terms"

    response = submit(client, "/logout", {})
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert client.get(APP_PATH).headers["location"] == f"/login?next={APP_PATH}"
    assert submit(browser(app), "/logout", {}).status_code == 303  # signing out twice is harmless


def test_sign_in_with_wrong_password_or_unknown_email_shows_one_message(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    signed_up(app, mailer)
    client = browser(app)

    wrong = submit(client, "/login", {"email": EMAIL, "password": "wrong password!!"})
    unknown = submit(client, "/login", {"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert "Wrong email or password." in wrong.text and 'value="ana@example.com"' in wrong.text


def test_sign_in_with_invalid_fields_shows_field_errors(app: FastAPI) -> None:
    invalid = submit(browser(app), "/login", {"email": "nope", "password": ""})
    assert invalid.status_code == 422 and "This field is required." in invalid.text


def test_login_page_with_next_keeps_it_in_the_form(app: FastAPI) -> None:
    page = browser(app).get("/login?next=/invitations/abc").text
    assert 'value="/invitations/abc"' in page


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "/\\evil.example"])
def test_sign_in_with_foreign_next_redirects_to_the_app(
    app: FastAPI, mailer: RecordingMailer, target: str
) -> None:
    signed_up(app, mailer)
    form = {"email": EMAIL, "password": PASSWORD, "next": target}
    assert submit(browser(app), "/login", form).headers["location"] == APP_PATH


def test_safe_next_with_local_path_keeps_its_query() -> None:
    assert safe_next("/app?tab=1") == "/app?tab=1"


def test_redirect_for_htmx_request_uses_hx_redirect(app: FastAPI) -> None:
    client = browser(app)
    response = submit(
        client,
        "/register",
        {"email": EMAIL, "display_name": "Ana", "password": PASSWORD, "consent": "on"},
        **{"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/register/sent"


def test_password_reset_from_link_changes_the_password(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    signed_up(app, mailer)
    client = browser(app)
    assert client.get("/forgot-password").status_code == 200

    invalid = submit(client, "/forgot-password", {"email": "nope"})
    assert invalid.status_code == 422 and "Enter a valid email address." in invalid.text

    sent = len(mailer.sent)
    unknown = submit(client, "/forgot-password", {"email": "nobody@example.com"})
    known = submit(client, "/forgot-password", {"email": EMAIL})
    assert unknown.status_code == known.status_code == 200
    assert unknown.text == known.text  # web.auth.no-enumeration
    assert len(mailer.sent) == sent + 1

    reset_path = link(mailer, EMAIL)
    assert reset_path.startswith("/reset-password#token=")
    token = token_in(reset_path)
    assert "data-token-form" in client.get("/reset-password").text
    assert f'value="{token}"' in client.get(f"/reset-password?token={token}").text  # older links

    weak = submit(client, "/reset-password", {"token": token, "password": "short"})
    assert weak.status_code == 422 and "Use at least 12 characters." in weak.text
    no_token = submit(client, "/reset-password", {"token": "", "password": PASSWORD + "!"})
    assert no_token.status_code == 422
    assert 'id="token-error">This field is required.' in no_token.text

    done = submit(client, "/reset-password", {"token": token, "password": PASSWORD + "!"})
    assert done.status_code == 200 and "Password changed" in done.text
    reused = submit(client, "/reset-password", {"token": token, "password": PASSWORD + "!"})
    assert reused.status_code == 400
    assert 'id="token-error"' in reused.text and "Request a new link" in reused.text

    signed_in = submit(client, "/login", {"email": EMAIL, "password": PASSWORD + "!"})
    assert signed_in.status_code == 303


@pytest.mark.parametrize(
    ("path", "data"),
    [
        (
            "/register",
            {"email": EMAIL, "display_name": "Ana", "password": PASSWORD, "consent": "on"},
        ),
        ("/login", {"email": EMAIL, "password": PASSWORD}),
        ("/verify", {"token": "nope"}),
        ("/forgot-password", {"email": EMAIL}),
        ("/reset-password", {"token": "nope", "password": PASSWORD}),
    ],
)
def test_forms_after_too_many_posts_answer_429(
    app: FastAPI, path: str, data: dict[str, str]
) -> None:
    client = browser(app)
    for _ in range(10):
        assert submit(client, path, data).status_code != 429
    response = submit(client, path, data)
    assert response.status_code == 429 and "Too many attempts" in response.text


def stale_htmx_post(client: TestClient, origin: str) -> Response:
    """An htmx form post whose token no longer matches the cookie, as after the cookie changed."""
    client.get("/login")
    headers = {"HX-Request": "true", "origin": origin}
    return client.post(
        "/forgot-password", data={"csrf_token": "old", "email": EMAIL}, headers=headers
    )


def test_forms_htmx_with_stale_token_keep_the_page_and_send_a_fresh_one(app: FastAPI) -> None:
    client = browser(app)
    response = stale_htmx_post(client, ORIGIN)
    event = json.loads(response.headers["HX-Trigger"])["sreditor-form-renewed"]
    assert response.status_code == 403 and response.headers["HX-Reswap"] == "none"
    assert event["token"] == client.cookies[CSRF_COOKIE] and "renewed" in event["message"]


def test_forms_htmx_from_another_site_get_the_expired_page(app: FastAPI) -> None:
    response = stale_htmx_post(browser(app), "https://evil.example")
    assert "HX-Trigger" not in response.headers and "This form has expired" in response.text


def test_forms_after_too_many_posts_carry_retry_after(app: FastAPI) -> None:
    client = browser(app)
    data = {"email": EMAIL}
    for _ in range(10):
        submit(client, "/forgot-password", data)
    response = submit(client, "/forgot-password", data)
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0


def test_invitation_from_link_joins_only_the_invited_account(
    app: FastAPI, mailer: RecordingMailer, sign_up: Callable[..., TestClient]
) -> None:
    owner = sign_up("owner@example.com", "Owner")
    assert owner.post("/channels", json=CHANNEL).status_code == 201
    invite = {"email": EMAIL, "role": "editor"}
    assert owner.post("/channels/funny-memes/invitations", json=invite).status_code == 201
    path = link(mailer, EMAIL)
    assert path.startswith("/invitations#token=")

    # The page opens the invitation by posting the fragment's token; no URL carries it after.
    visitor = browser(app)
    assert "data-auto-submit" in visitor.get("/invitations").text
    opened = open_invitation(visitor, path)
    assert (opened.status_code, opened.headers["location"]) == (303, "/invitations")
    page = visitor.get("/invitations")
    assert "Join funny-memes" in page.text and "editor" in page.text
    assert 'href="/login?next=/invitations"' in page.text
    join = submit(visitor, "/invitations/join", {})
    assert join.headers["location"] == "/login?next=/invitations"

    stranger = signed_up(app, mailer, "other@example.com")
    open_invitation(stranger, path)
    wrong_account = stranger.get("/invitations").text
    assert "but this invitation is for ana@example.com" in wrong_account
    assert "Accept invitation" not in wrong_account
    assert "Sign out and continue as ana@example.com" in wrong_account
    wrong = submit(stranger, "/invitations/join", {})
    assert wrong.status_code == 403 and "for another email" in wrong.text

    invitee = signed_up(app, mailer)
    open_invitation(invitee, path)
    assert "Accept invitation" in invitee.get("/invitations").text
    accepted = submit(invitee, "/invitations/join", {})
    assert (accepted.status_code, accepted.headers["location"]) == (303, APP_PATH)
    home = invitee.get(APP_PATH).text
    assert 'href="/app/channels/funny-memes">Funny Memes' in home
    assert '<span class="badge">Editor</span>' in home
    assert submit(invitee, "/invitations/join", {}).status_code == 404  # the cookie is gone


def test_invitation_open_with_unknown_token_shows_the_error_at_the_field(app: FastAPI) -> None:
    client = browser(app)
    refused = submit(client, "/invitations/open", {"token": "nope"})
    assert refused.status_code == 404 and 'id="token-error"' in refused.text


def test_invitation_page_with_expired_cookie_asks_for_the_link(app: FastAPI) -> None:
    client = browser(app)
    client.cookies.set("sreditor_invitation", "gone", path="/invitations")
    page = client.get("/invitations")
    assert page.status_code == 404 and 'id="token-error"' in page.text


def test_invitation_legacy_link_moves_the_token_to_a_cookie(
    app: FastAPI,
) -> None:
    response = browser(app).get("/invitations/old-token")
    assert (response.status_code, response.headers["location"]) == (303, "/invitations")
    assert "sreditor_invitation=old-token" in response.headers["set-cookie"]


def test_invitation_open_after_too_many_posts_answers_429(app: FastAPI) -> None:
    client = browser(app)
    for _ in range(10):
        submit(client, "/invitations/open", {"token": "nope"})
    response = submit(client, "/invitations/open", {"token": "nope"})
    assert response.status_code == 429 and "Too many attempts" in response.text


def stmt_create(
    store: StoreSettings, statement_id: str, category: str, text: str, priority: int = 0
) -> None:
    env = {**os.environ, "STMT_DB": store.db, "STMT_DOMAIN": store.domain, "STMT_SCOPE": "/"}
    owner = {"type": "user", "id": "t"}
    document = json.dumps(
        {"statement": text, "owner": owner, "category": category, "priority": priority}
    )
    subprocess.run(
        [store.stmt, "create", statement_id, "--json", document],
        env=env,
        check=True,
        capture_output=True,
    )


def test_legal_pages_in_english_show_store_statements(app: FastAPI, store: StoreSettings) -> None:
    client = browser(app)
    assert "This page has not been written yet." in client.get("/terms").text

    stmt_create(store, "terms.20.use", "terms", "Use it well.")
    stmt_create(store, "terms.10.intro", "terms", "First paragraph.")
    stmt_create(store, "privacy.10.data", "privacy", "We keep your email.")

    terms = client.get("/terms").text
    assert "<h1>Terms of service</h1>" in terms
    assert terms.index("First paragraph.") < terms.index("Use it well.")
    assert "We keep your email." not in terms
    assert "We keep your email." in client.get("/privacy").text


def test_legal_pages_order_sections_by_priority_then_id(app: FastAPI, store: StoreSettings) -> None:
    stmt_create(store, "terms.a.late", "terms", "Comes last.", priority=2)
    stmt_create(store, "terms.b.early", "terms", "Comes first.", priority=1)
    terms = browser(app).get("/terms").text
    assert terms.index("Comes first.") < terms.index("Comes last.")


def test_redirect_with_lang_parameter_remembers_the_choice(app: FastAPI) -> None:
    client = browser(app)
    response = client.get("/app?lang=pt-BR", follow_redirects=False)
    assert response.status_code == 303
    assert "sreditor_lang=pt-BR" in response.headers["set-cookie"]


class Pair(BaseModel):
    low: int
    high: int

    @model_validator(mode="after")
    def ordered(self) -> Pair:
        if self.low > self.high:
            raise ValueError("low must not exceed high")
        return self


def test_field_errors_for_model_level_error_go_to_the_form() -> None:
    with pytest.raises(ValidationError) as caught:
        Pair(low=2, high=1)
    assert field_errors(caught.value, Translator()) == {"form": "low must not exceed high"}


def test_message_for_unknown_error_type_hides_pydantic_text() -> None:
    error = {"type": "bool_parsing", "msg": "Input should be a valid boolean"}
    assert message_for(error, Translator("pt-BR")) == "Confira este valor."


def test_pages_with_portuguese_accept_language_are_in_portuguese(app: FastAPI) -> None:
    client = browser(app)
    client.headers["accept-language"] = "pt-BR,pt;q=0.9,en;q=0.8"

    landing = client.get("/").text
    assert '<html lang="pt-BR">' in landing
    assert "Gerencie todos os seus canais em um só lugar." in landing
    assert "proprietário, editor, colaborador ou leitor" in landing
    assert '<span aria-current="true">Português (Brasil)</span>' in landing

    errors = register(client, "nope", password="short", consent="")
    assert "Informe um e-mail válido." in errors.text
    assert "Use pelo menos 12 caracteres." in errors.text
    assert "Aceite os termos de uso" in errors.text

    wrong = submit(client, "/login", {"email": EMAIL, "password": "wrong password!!"})
    assert "E-mail ou senha incorretos." in wrong.text
    expired = client.post("/logout", data={"csrf_token": "forged"})
    assert "Este formulário expirou" in expired.text


def test_language_switch_with_lang_parameter_is_remembered(app: FastAPI) -> None:
    client = browser(app)
    assert '<html lang="en">' in client.get("/").text

    switched = client.get("/verify?token=abc&lang=pt-BR")
    assert '<html lang="pt-BR">' in switched.text
    assert 'value="abc"' in switched.text  # switching keeps the rest of the address
    assert '<a href="/verify?token=abc&amp;lang=en" rel="nofollow">English</a>' in switched.text
    assert "<h1>Confira seu e-mail</h1>" in client.get("/register/sent").text
    assert '<html lang="pt-BR">' in client.get("/").text  # remembered by the cookie
    client.get("/?lang=en")
    assert '<html lang="en">' in client.get("/").text
    assert '<html lang="en">' in client.get("/?lang=fr").text  # unknown: keep the choice


def test_emails_for_portuguese_request_are_in_portuguese(
    app: FastAPI, mailer: RecordingMailer, sign_up: Callable[..., TestClient]
) -> None:
    client = browser(app)
    client.headers["accept-language"] = "pt-BR"
    register(client)
    _to, subject, body = mailer.sent[-1]
    assert subject == "Confirme seu e-mail"
    assert body.startswith("Confirme seu e-mail em até dois dias: https://sreditor.test/verify")

    owner = sign_up("owner@example.com", "Owner")
    assert owner.post("/channels", json=CHANNEL).status_code == 201
    invite = {"email": "guest@example.com", "role": "viewer"}
    response = owner.post(
        "/channels/funny-memes/invitations", json=invite, headers={"accept-language": "pt"}
    )
    assert response.status_code == 201
    _to, subject, body = mailer.sent[-1]
    assert subject == "Owner convidou você para Funny Memes"
    assert "como leitor" in body

    # The JSON API itself stays in English (i18n.api.english).
    api = TestClient(app, headers={"accept-language": "pt-BR"})
    refused = api.post("/auth/login", json={"email": EMAIL, "password": "wrong password!!"})
    assert refused.json()["detail"] == "Wrong email or password."


def test_role_names_on_portuguese_pages_are_translated(
    app: FastAPI, mailer: RecordingMailer, sign_up: Callable[..., TestClient]
) -> None:
    owner = sign_up("owner@example.com", "Owner")
    assert owner.post("/channels", json=CHANNEL).status_code == 201
    invite = {"email": EMAIL, "role": "contributor"}
    assert owner.post("/channels/funny-memes/invitations", json=invite).status_code == 201
    path = link(mailer, EMAIL)

    invitee = signed_up(app, mailer)
    invitee.headers["accept-language"] = "pt-BR"
    open_invitation(invitee, path)
    assert "<dd>colaborador</dd>" in invitee.get("/invitations").text
    submit(invitee, "/invitations/join", {})
    home = invitee.get(APP_PATH).text
    assert "Seus canais" in home and '<span class="badge">Colaborador</span>' in home


def test_legal_pages_in_portuguese_use_the_translated_category(
    app: FastAPI, store: StoreSettings
) -> None:
    client = browser(app)
    client.headers["accept-language"] = "pt-BR"
    stmt_create(store, "terms.10.intro", "terms", "English terms.")
    terms = client.get("/terms").text
    assert "<h1>Termos de uso</h1>" in terms and "English terms." in terms  # fallback

    stmt_create(store, "terms.pt-br.10.intro", "terms.pt-br", "Termos em português.")
    terms = client.get("/terms").text
    assert "Termos em português." in terms and "English terms." not in terms
    assert "English terms." in browser(app).get("/terms").text


def test_logout_with_local_next_redirects_there(app: FastAPI, mailer: RecordingMailer) -> None:
    client = signed_up(app, mailer)
    response = submit(client, "/logout", {"next": "/login?next=/invitations/abc"})
    assert response.headers["location"] == "/login?next=/invitations/abc"
    assert SESSION_COOKIE not in client.cookies


def test_logout_with_foreign_next_redirects_to_the_app(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    client = signed_up(app, mailer)
    response = submit(client, "/logout", {"next": "https://evil.example"})
    assert response.headers["location"] == APP_PATH


def test_header_for_signed_in_user_shows_the_name_on_every_page(
    app: FastAPI, mailer: RecordingMailer
) -> None:
    client = signed_up(app, mailer)
    terms = client.get("/terms").text
    assert '<a href="/app">Ana</a>' in terms and "Sign out" in terms
    assert 'href="/register"' not in terms
