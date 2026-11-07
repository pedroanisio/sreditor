"""Every public flow in a real browser, in each language (rules ui.e2e.playwright, ui.e2e.evidence)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import APIRequestContext, Browser, Page, Playwright, expect

from e2e.conftest import Evidence, Server, evidence_dir
from sreditor.i18n import LOCALE_NAMES, LOCALES, Translator
from sreditor.members import Role

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new passphrase"
CHANNEL = {"slug": "funny-memes", "name": "Funny Memes"}
TOKEN_IN_URL = re.compile(r"token=")
OWNER_EMAIL = "owner@example.com"


@pytest.fixture(params=LOCALES)
def translate(request: pytest.FixtureRequest) -> Translator:
    return Translator(request.param)


def open_page(page: Page, server: Server, path: str, translate: Translator) -> None:
    """Open a page; the first visit picks the language with the ?lang= switch."""
    separator = "&" if "?" in path else "?"
    page.goto(f"{server.url}{path}{separator}lang={translate.locale}")
    expect(page.locator("html")).to_have_attribute("lang", translate.locale)


def heading(page: Page, text: str) -> None:
    expect(page.get_by_role("heading", level=1, name=text)).to_be_visible()


def register(
    page: Page, server: Server, translate: Translator, email: str, evidence: Evidence
) -> None:
    open_page(page, server, "/register", translate)
    heading(page, translate("Create your account"))
    evidence.capture("register")
    page.get_by_role("button", name=translate("Create account")).click()
    expect(page.locator("#consent-error")).to_have_text(
        translate("Accept the terms of service and privacy policy to continue.")
    )
    evidence.capture("register-errors")
    page.get_by_label(translate("Your name")).fill("Ana")
    page.get_by_label(translate("Email")).fill(email)
    page.get_by_label(translate("Password")).fill(PASSWORD)
    page.locator("#consent").check()
    page.get_by_role("button", name=translate("Create account")).click()
    heading(page, translate("Check your email"))
    evidence.capture("register-sent")


def verify(
    page: Page, server: Server, translate: Translator, email: str, evidence: Evidence
) -> None:
    page.goto(server.link_for(email))
    heading(page, translate("Confirm your email"))
    # token.js moved the token from the fragment into the form and out of the address bar.
    expect(page).not_to_have_url(TOKEN_IN_URL)
    expect(page.get_by_label(translate("Link from your email"))).to_be_hidden()
    evidence.capture("verify")
    page.get_by_role("button", name=translate("Confirm email")).click()
    heading(page, translate("Email verified"))
    evidence.capture("verified")


def sign_in(page: Page, translate: Translator, email: str, password: str) -> None:
    page.get_by_label(translate("Email")).fill(email)
    page.get_by_label(translate("Password")).fill(password)
    page.get_by_role("button", name=translate("Sign in")).click()


def sign_out(page: Page, translate: Translator, name: str) -> None:
    """Sign out from the user menu in the workspace header."""
    page.locator("summary").get_by_text(name, exact=True).click()
    page.get_by_role("button", name=translate("Sign out")).click()


def test_registration_in_browser_reaches_sign_in_and_out(
    page: Page, server: Server, translate: Translator, evidence: Evidence
) -> None:
    email = "ana@example.com"
    open_page(page, server, "/", translate)
    heading(page, translate("Run every channel from one place."))
    evidence.capture("landing")
    register(page, server, translate, email, evidence)
    verify(page, server, translate, email, evidence)
    page.get_by_role("link", name=translate("Sign in")).last.click()
    sign_in(page, translate, email, "not the password")
    expect(page.get_by_role("alert")).to_have_text(translate("Wrong email or password."))
    evidence.capture("sign-in-refused")
    sign_in(page, translate, email, PASSWORD)
    heading(page, translate("Your channels"))
    evidence.capture("signed-in")
    sign_out(page, translate, "Ana")
    heading(page, translate("Run every channel from one place."))
    evidence.capture("signed-out")


def test_password_reset_in_browser_lets_the_new_password_sign_in(
    page: Page, server: Server, translate: Translator, evidence: Evidence
) -> None:
    email = "bia@example.com"
    register(page, server, translate, email, evidence)
    verify(page, server, translate, email, evidence)
    open_page(page, server, "/forgot-password", translate)
    heading(page, translate("Reset your password"))
    evidence.capture("forgot")
    page.get_by_label(translate("Email")).fill(email)
    page.get_by_role("button", name=translate("Send link")).click()
    heading(page, translate("Check your email"))
    evidence.capture("forgot-sent")
    page.goto(server.link_for(email))
    heading(page, translate("Choose a new password"))
    page.get_by_label(translate("New password")).fill("short")
    page.get_by_role("button", name=translate("Save password")).click()
    expect(page.locator("#password-error")).to_have_text(
        translate("Use at least %(count)s characters.", count=12)
    )
    evidence.capture("reset-errors")
    page.get_by_label(translate("New password")).fill(NEW_PASSWORD)
    page.get_by_role("button", name=translate("Save password")).click()
    heading(page, translate("Password changed"))
    evidence.capture("reset-done")
    page.get_by_role("link", name=translate("Sign in")).last.click()
    sign_in(page, translate, email, NEW_PASSWORD)
    heading(page, translate("Your channels"))


def invite(api: APIRequestContext, server: Server, invitee: str) -> None:
    """An owner creates a channel and invites `invitee`, through the JSON API."""
    owner = {"email": OWNER_EMAIL, "display_name": "Olga", "password": PASSWORD}
    assert api.post(f"{server.url}/auth/register", data=owner).ok
    token = parse_qs(urlsplit(server.link_for(owner["email"])).fragment)["token"][0]
    assert api.post(f"{server.url}/auth/verify", data={"token": token}).ok
    login = api.post(f"{server.url}/auth/login", data=owner).json()
    headers = {"Authorization": f"Bearer {login['token']}"}
    assert api.post(f"{server.url}/channels", data=CHANNEL, headers=headers).ok
    invitation = {"email": invitee, "role": Role.EDITOR.value}
    path = f"{server.url}/channels/{CHANNEL['slug']}/invitations"
    assert api.post(path, data=invitation, headers=headers).ok


def test_invitation_in_browser_joins_the_channel(
    page: Page, playwright: Playwright, server: Server, translate: Translator, evidence: Evidence
) -> None:
    email = "caio@example.com"
    # Its own cookie jar: the owner's API sign-in must not sign the browser in.
    api = playwright.request.new_context()
    invite(api, server, email)
    api.dispose()
    invitation_link = server.link_for(email)
    fragment = urlsplit(invitation_link).fragment
    page.goto(f"{server.url}/invitations?lang={translate.locale}#{fragment}")
    heading(page, translate("Join %(channel)s", channel=CHANNEL["slug"]))
    expect(page).not_to_have_url(TOKEN_IN_URL)
    evidence.capture("invitation")
    register(page, server, translate, email, evidence)
    verify(page, server, translate, email, evidence)
    page.goto(invitation_link)
    page.get_by_role("link", name=translate("Sign in to accept")).click()
    # Signed in with the wrong account, the page offers to switch instead of a failing button.
    sign_in(page, translate, OWNER_EMAIL, PASSWORD)
    heading(page, translate("Join %(channel)s", channel=CHANNEL["slug"]))
    expect(page.get_by_role("button", name=translate("Accept invitation"))).to_have_count(0)
    evidence.capture("invitation-wrong-account")
    page.get_by_role(
        "button", name=translate("Sign out and continue as %(email)s", email=email)
    ).click()
    heading(page, translate("Sign in"))
    sign_in(page, translate, email, PASSWORD)
    heading(page, translate("Join %(channel)s", channel=CHANNEL["slug"]))
    evidence.capture("invitation-signed-in")
    page.get_by_role("button", name=translate("Accept invitation")).click()
    heading(page, translate("Your channels"))
    channels = page.get_by_role("list", name=translate("Your channels"))
    expect(channels).to_contain_text(CHANNEL["name"])
    expect(channels).to_contain_text(translate.role(Role.EDITOR).capitalize())
    evidence.capture("invitation-accepted")


def test_verify_without_javascript_accepts_a_pasted_link(
    browser: Browser, server: Server, translate: Translator, request: pytest.FixtureRequest
) -> None:
    """Without JavaScript nothing reads the fragment, so the page asks for the link instead."""
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    evidence = Evidence(page, evidence_dir(request.node.name))
    email = "dora@example.com"
    register(page, server, translate, email, evidence)
    verify_link = server.link_for(email)
    page.goto(verify_link)
    heading(page, translate("Confirm your email"))
    field = page.get_by_label(translate("Link from your email"))
    expect(field).to_be_visible()
    field.fill(verify_link)
    evidence.capture("verify-pasted")
    page.get_by_role("button", name=translate("Confirm email")).click()
    heading(page, translate("Email verified"))
    evidence.capture("verified")
    context.close()


def test_language_switch_in_browser_translates_and_is_remembered(
    page: Page, server: Server, evidence: Evidence
) -> None:
    page.goto(server.url)
    for locale in reversed(LOCALES):
        page.get_by_role("link", name=LOCALE_NAMES[locale]).click()
        expect(page.locator("html")).to_have_attribute("lang", locale)
        heading(page, Translator(locale)("Run every channel from one place."))
        evidence.capture(f"switched-{locale}")
        page.goto(f"{server.url}/login")
        expect(page.locator("html")).to_have_attribute("lang", locale)  # remembered
        page.goto(server.url)


def test_design_page_in_browser_shows_every_component(
    page: Page, server: Server, translate: Translator, evidence: Evidence
) -> None:
    open_page(page, server, "/design", translate)
    heading(page, translate("Design system"))
    evidence.capture("design")
