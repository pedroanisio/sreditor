"""The signed-in workspace in a real browser, in each language (rules ui.e2e.playwright, ui.e2e.evidence)."""

from __future__ import annotations

import io
import re
import wave
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image
from playwright.sync_api import Locator, Page, Playwright, expect

from e2e.conftest import Evidence, Server
from e2e.test_flows import PASSWORD, heading, open_page, sign_in
from sreditor.i18n import LOCALES, Translator

EMAIL = "ana@example.com"
DESCRIPTION_LIMIT = 80  # TikTok's, in sreditor.channels.DESCRIPTION_LIMITS
AVATAR_SIZE = 64
# A voice-over take: 8 kHz, 8-bit mono, where 128 is silence.
VOICE_RATE = 8000
SILENCE = 128


PITCH_TITLE = "Why Mondays hurt"
PITCH_SOURCE = "Sleep debt study"


@pytest.fixture(params=LOCALES)
def translate(request: pytest.FixtureRequest) -> Translator:
    return Translator(request.param)


def avatar() -> bytes:
    """A small square PNG, as a person would upload."""
    buffer = io.BytesIO()
    Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), "#ff0066").save(buffer, "PNG")
    return buffer.getvalue()


def narration() -> bytes:
    """A one-second silent WAV file, standing in for a voice-over take."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as take:
        take.setnchannels(1)
        take.setsampwidth(1)
        take.setframerate(VOICE_RATE)
        take.writeframes(bytes([SILENCE]) * VOICE_RATE)
    return buffer.getvalue()


def registered(playwright: Playwright, server: Server) -> None:
    """Register and verify Ana through the JSON API, so the journey starts at sign-in."""
    api = playwright.request.new_context()
    person = {"email": EMAIL, "display_name": "Ana", "password": PASSWORD}
    assert api.post(f"{server.url}/auth/register", data=person).ok
    token = parse_qs(urlsplit(server.link_for(EMAIL)).fragment)["token"][0]
    assert api.post(f"{server.url}/auth/verify", data={"token": token}).ok
    api.dispose()


def flash(page: Page, text: str) -> None:
    """The note a saved change leaves on the next page, in a toast."""
    expect(page.locator(".toast")).to_have_text(text)


def open_dialog(page: Page, name: str) -> Locator:
    """Open a form's dialog with its button, and return the dialog."""
    page.locator(f"[data-dialog='{name}']").first.click()
    dialog = page.locator(f"dialog#{name}")
    expect(dialog).to_be_visible()
    return dialog


def section(page: Page, translate: Translator, name: str) -> None:
    """Open one of the channel's sections from its side menu."""
    menu = page.get_by_role("navigation", name=translate("Channel sections"))
    menu.get_by_role("link", name=translate(name)).click()
    heading(page, translate(name) if name != "Brand" else translate("Brand kit"))


def form_with(page: Page, field: str) -> Locator:
    """The form that holds a field, for pages with one form per item."""
    return page.locator("form", has=page.locator(field))


def create_channel(page: Page, translate: Translator, evidence: Evidence) -> None:
    heading(page, translate("Your channels"))
    evidence.capture("no-channels")
    page.get_by_role("main").get_by_role("link", name=translate("New channel")).click()
    heading(page, translate("New channel"))
    page.get_by_label(translate("Name")).fill("Funny Memes")
    evidence.capture("new-channel")
    page.get_by_role("button", name=translate("Create channel")).click()
    heading(page, translate("Accounts"))
    flash(page, translate("Channel created. Add its accounts next."))
    evidence.capture("channel-created")


def add_account(page: Page, translate: Translator, evidence: Evidence) -> None:
    dialog = open_dialog(page, "add-account")
    evidence.capture("add-account-dialog")
    dialog.locator("#new-platform").select_option("tiktok")
    dialog.locator("#new-handle").fill("funnymemes")
    dialog.locator("#new-description").fill("x" * (DESCRIPTION_LIMIT + 1))
    dialog.get_by_role("button", name=translate("Add account")).click()
    expect(page.locator("#new-description-error")).to_have_text(
        translate(
            "%(platform)s allows %(limit)s characters; this has %(count)s.",
            platform="TikTok",
            limit=DESCRIPTION_LIMIT,
            count=DESCRIPTION_LIMIT + 1,
        )
    )
    evidence.capture("account-errors")
    # The page came back with the error, and its dialog opened again.
    dialog = page.locator("dialog#add-account")
    dialog.locator("#new-description").fill("Memes every day.")
    dialog.get_by_role("button", name=translate("Add account")).click()
    flash(page, translate("Changes saved."))
    evidence.capture("account-list")
    open_dialog(page, "edit-tiktok")
    counter = page.locator("[data-counter-for='tiktok-description']")
    expect(counter).to_have_text(f"16/{DESCRIPTION_LIMIT}")
    page.locator("#tiktok-description").fill("x" * (DESCRIPTION_LIMIT + 1))
    expect(counter).to_have_class("counter over")  # workspace.js keeps it live
    evidence.capture("account-added")
    page.keyboard.press("Escape")
    expect(page.locator("dialog#edit-tiktok")).to_be_hidden()


def set_up_brand(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Brand")
    evidence.capture("brand-empty")
    upload = open_dialog(page, "upload-avatar")
    upload.locator("#avatar-file").set_input_files(
        {"name": "avatar.png", "mimeType": "image/png", "buffer": avatar()}
    )
    upload.get_by_role("button", name=translate("Upload")).click()
    flash(page, translate("File uploaded."))
    expect(page.locator("img.preview")).to_be_visible()
    page.locator("#color-name").fill("Pink")
    page.locator("#color-hex").fill("#ff0066")
    page.get_by_role("button", name=translate("Save color")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator(".chip")).to_contain_text("#ff0066")
    evidence.capture("brand")
    page.get_by_role("navigation", name=translate("Brand kit levels")).get_by_role(
        "link", name="TikTok"
    ).click()
    expect(page.get_by_text(translate("From the channel")).first).to_be_visible()
    evidence.capture("brand-tiktok")


def write_editorial(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Editorial")
    purpose = form_with(page, "#purpose-text")
    purpose.locator("#purpose-text").fill("Make people laugh every day.")
    # The form expires (its cookie is gone): the page stays, says so and keeps the text.
    page.context.clear_cookies(name="sreditor_csrf")
    purpose.get_by_role("button", name=translate("Save")).click()
    expect(purpose.locator("[data-renewed]")).to_have_text(
        translate("This form had expired, so it was renewed. Submit it again.")
    )
    expect(purpose.locator("#purpose-text")).to_have_value("Make people laugh every day.")
    evidence.capture("form-renewed")
    purpose.get_by_role("button", name=translate("Save")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator("#kind-purpose")).to_contain_text(translate("Set here"))
    evidence.capture("editorial")


def produce_episode(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Episodes")
    evidence.capture("episodes-empty")
    form = page.locator("#new-episode")
    form.get_by_label(translate("Title")).fill("Pilot")
    form.get_by_label(translate("Summary")).fill("Why Mondays hurt.")
    form.get_by_role("button", name=translate("Add episode")).click()
    flash(page, translate("Episode created."))
    heading(page, "#1 · Pilot")
    evidence.capture("episode-created")
    page.get_by_label(translate("Status")).select_option("script")
    page.get_by_role("button", name=translate("Save changes")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator(".page-header .badge")).to_have_text(translate("Script"))
    evidence.capture("episode-saved")
    research_episode(page, translate, evidence)


def research_episode(page: Page, translate: Translator, evidence: Evidence) -> None:
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Research")).click()
    notes = page.locator("#notes")
    notes.get_by_label(translate("Research notes")).fill(
        "Mondays feel longer.\nAsk a sleep expert."
    )
    notes.get_by_role("button", name=translate("Save notes")).click()
    flash(page, translate("Changes saved."))
    evidence.capture("research-notes")
    source = page.locator("#new-source")
    source.get_by_label(translate("Link")).fill("https://example.com/mondays")
    source.get_by_label(translate("Title")).fill("Mondays study")
    source.get_by_label(translate("What it supports")).fill("Mondays feel longer.")
    source.get_by_role("button", name=translate("Add source")).click()
    flash(page, translate("Source added."))
    expect(page.locator("#sources")).to_contain_text("Mondays study")
    evidence.capture("research-source")
    link_pitch(page, translate, evidence)


def link_pitch(page: Page, translate: Translator, evidence: Evidence) -> None:
    """Create a pitch with a source, then build the episode on it and see its research there."""
    research_url = page.url
    section(page, translate, "Pitches")
    evidence.capture("pitches-empty")
    form = page.locator("#new-pitch")
    form.get_by_label(translate("Title")).fill(PITCH_TITLE)
    form.get_by_label(translate("Summary")).fill("The science of the Monday slump.")
    form.get_by_role("button", name=translate("Add pitch")).click()
    flash(page, translate("Pitch created."))
    heading(page, PITCH_TITLE)
    source = page.locator("#new-source")
    source.get_by_label(translate("Link")).fill("https://example.com/sleep")
    source.get_by_label(translate("Title")).fill(PITCH_SOURCE)
    source.get_by_label(translate("What it supports")).fill("Sleep debt peaks on Mondays.")
    source.get_by_role("button", name=translate("Add source")).click()
    flash(page, translate("Source added."))
    expect(page.locator("#sources")).to_contain_text(PITCH_SOURCE)
    evidence.capture("pitch")
    page.goto(research_url)
    chosen = page.locator("#pitch")
    chosen.get_by_label(translate("Pitch this episode is built on")).select_option(
        label=PITCH_TITLE
    )
    chosen.get_by_role("button", name=translate("Save pitch")).click()
    flash(page, translate("Changes saved."))
    # The pitch's research shows on the episode as it is on the pitch, never copied.
    expect(page.locator("#pitch")).to_contain_text(PITCH_SOURCE)
    evidence.capture("research-pitch")
    write_screenplay(page, translate, evidence)


def write_screenplay(page: Page, translate: Translator, evidence: Evidence) -> None:
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Screenplay")).click()
    evidence.capture("screenplay-empty")
    script = page.locator("#screenplay")
    script.get_by_label(translate("Fountain text")).fill(
        "INT. KITCHEN - DAY\n\nAna stares at the calendar.\n\nANA\nIs it Monday again?\n"
    )
    script.get_by_role("button", name=translate("Save screenplay")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator("#scenes")).to_contain_text("INT. KITCHEN - DAY")
    expect(page.locator("#characters")).to_contain_text("ANA")
    evidence.capture("screenplay")
    add_cast(page, translate, evidence)
    upload_voice_over(page, translate, evidence)
    upload_waiver(page, translate, evidence)
    check_library(page, translate, evidence)
    pick_style(page, translate, evidence)


def add_cast(page: Page, translate: Translator, evidence: Evidence) -> None:
    characters = page.locator("#characters")
    characters.get_by_role("button", name=translate("Add to library")).click()
    flash(page, translate("Added to the library."))
    expect(characters).to_contain_text(translate("In the library"))
    evidence.capture("screenplay-cast")


def upload_voice_over(page: Page, translate: Translator, evidence: Evidence) -> None:
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Media")).click()
    evidence.capture("media-empty")
    upload = open_dialog(page, "upload-voice-over")
    upload.locator("#voice-over-file").set_input_files(
        {"name": "intro take 1.wav", "mimeType": "audio/wav", "buffer": narration()}
    )
    upload.get_by_role("button", name=translate("Upload")).click()
    flash(page, translate("File uploaded."))
    voice_over = page.locator("#voice-over")
    expect(voice_over).to_contain_text("intro take 1")
    player = voice_over.locator("audio.player")
    # The browser reads the duration from the file the page serves, so it is really playable.
    expect(player).to_have_js_property("duration", 1)
    evidence.capture("media")


def release() -> bytes:
    """The first bytes of a PDF, standing in for a scanned, signed waiver."""
    return b"%PDF-1.7\n%signed\n"


def upload_waiver(page: Page, translate: Translator, evidence: Evidence) -> None:
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Waivers")).click()
    evidence.capture("waivers-empty")
    upload = open_dialog(page, "upload-waiver")
    upload.locator("#waiver-file").set_input_files(
        {"name": "bia.pdf", "mimeType": "application/pdf", "buffer": release()}
    )
    upload.locator("#waiver-signer").fill("Bia Rocha")
    upload.locator("#waiver-signed-on").fill("2026-11-01")
    upload.get_by_role("button", name=translate("Upload")).click()
    flash(page, translate("File uploaded."))
    waivers = page.locator("#waivers")
    expect(waivers).to_contain_text("Bia Rocha")
    # The download goes to the browser, not to htmx, and keeps a name people recognise.
    with page.expect_download() as saved:
        waivers.get_by_role("link", name=translate("Download")).click()
    assert saved.value.suggested_filename == "Bia Rocha.pdf"
    evidence.capture("waivers")


def check_library(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Library")
    expect(page.locator("#characters")).to_contain_text("ANA")
    dialog = open_dialog(page, "add-style")
    dialog.locator("#new-style-name").fill("Warm documentary")
    dialog.locator("#new-style-description").fill("Handheld, natural light, warm palette.")
    dialog.get_by_role("button", name=translate("Add style")).click()
    flash(page, translate("Added to the library."))
    expect(page.locator("#styles")).to_contain_text("Warm documentary")
    evidence.capture("library")


def pick_style(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Episodes")
    page.get_by_role("link", name=re.compile("Pilot")).first.click()
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Media")).click()
    style = page.locator("#style")
    style.get_by_label(translate("Style")).select_option(label="Warm documentary")
    style.get_by_role("button", name=translate("Save style")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator("#style")).to_contain_text("Handheld, natural light")
    evidence.capture("media-style")


def invite_member(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Members")
    dialog = open_dialog(page, "invite")
    dialog.locator("#invite-email").fill("bia@example.com")
    dialog.locator("#invite-role").select_option("editor")
    dialog.get_by_role("button", name=translate("Send invitation")).click()
    flash(page, translate("Invitation sent."))
    expect(page.locator("#invitations")).to_contain_text("bia@example.com")
    evidence.capture("members")


def create_token(page: Page, translate: Translator, evidence: Evidence) -> None:
    page.locator("summary").get_by_text("Ana", exact=True).click()
    page.get_by_role("link", name=translate("API tokens")).click()
    heading(page, translate("API tokens"))
    page.get_by_role("button", name=translate("Create token")).click()
    expect(page.locator("#scopes-error")).to_have_text(translate("Choose at least one permission."))
    page.get_by_label(translate("Name")).fill("Posting agent")
    page.get_by_label("channels:read").check()
    page.get_by_role("button", name=translate("Create token")).click()
    expect(page.locator("#secret")).to_have_value(re.compile(r"^sre_"))
    # Located by what it copies: its name changes from "Copy" to "Copied".
    copy = page.locator("[data-copy='secret']")
    expect(copy).to_have_text(translate("Copy"))
    copy.click()
    expect(copy).to_have_text(translate("Copied"))
    evidence.capture("token-created")


def delete_channel(page: Page, server: Server, translate: Translator, evidence: Evidence) -> None:
    open_page(page, server, "/app/channels/funny-memes/settings", translate)
    page.get_by_label(translate("Type %(slug)s to confirm", slug="funny-memes")).fill("funny-memes")
    evidence.capture("delete")
    page.get_by_role("button", name=translate("Delete this channel")).click()
    heading(page, translate("Your channels"))
    flash(page, translate("Channel deleted."))
    evidence.capture("deleted")


def test_workspace_in_browser_sets_up_a_channel_end_to_end(
    page: Page, playwright: Playwright, server: Server, translate: Translator, evidence: Evidence
) -> None:
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=server.url)
    registered(playwright, server)
    open_page(page, server, "/app", translate)
    sign_in(page, translate, EMAIL, PASSWORD)
    create_channel(page, translate, evidence)
    add_account(page, translate, evidence)
    set_up_brand(page, translate, evidence)
    write_editorial(page, translate, evidence)
    produce_episode(page, translate, evidence)
    invite_member(page, translate, evidence)
    create_token(page, translate, evidence)
    delete_channel(page, server, translate, evidence)
