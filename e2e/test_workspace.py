"""The signed-in workspace in a real browser, in each language (rules ui.e2e.playwright, ui.e2e.evidence)."""

from __future__ import annotations

import io
import re
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image
from playwright.sync_api import Locator, Page, Playwright, expect

from e2e.conftest import Evidence, Server
from e2e.test_flows import PASSWORD, heading, open_page, sign_in
from sreditor.i18n import LOCALES, Translator
from tests.media_samples import cube_glb

EMAIL = "ana@example.com"
DESCRIPTION_LIMIT = 80  # TikTok's, in sreditor.channels.DESCRIPTION_LIMITS
AVATAR_SIZE = 64
# A voice-over take: 8 kHz, 8-bit mono, where 128 is silence.
VOICE_RATE = 8000
SILENCE = 128


PITCH_TITLE = "Why Mondays hurt"
PITCH_SOURCE = "Sleep debt study"
SECOND_PITCH_TITLE = "Sunday scaries"
SECOND_PITCH_SOURCE = "Anticipatory anxiety review"
EPISODE_SOURCE = "Mondays study"
CLAIM_QUOTE = "Mondays feel longer than other days."
POST_LINK = "https://www.tiktok.com/@funnymemes/video/1"
VIEWER_TIMEOUT_MS = 30_000


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
    page.locator("#color-role").select_option("primary")
    page.get_by_role("button", name=translate("Save color")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator(".chip")).to_contain_text("#ff0066")
    expect(page.locator("#colors")).to_contain_text(translate("Primary"))
    set_up_fonts(page, translate)
    evidence.capture("brand")
    page.get_by_role("navigation", name=translate("Brand kit levels")).get_by_role(
        "link", name="TikTok"
    ).click()
    expect(page.get_by_text(translate("From the channel")).first).to_be_visible()
    evidence.capture("brand-tiktok")


def set_up_fonts(page: Page, translate: Translator) -> None:
    """Set the heading and body fonts, each with its family and weight."""
    for role, family, weight in (("heading", "Inter", "700"), ("body", "Lora", "400")):
        dialog = open_dialog(page, f"font-{role}")
        dialog.locator(f"#{role}-family").fill(family)
        dialog.locator(f"#{role}-weight").select_option(weight)
        dialog.get_by_role("button", name=translate("Save font")).click()
        flash(page, translate("Changes saved."))
        expect(page.locator("#fonts")).to_contain_text(f"{family} · {weight}")


def write_editorial(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Editorial")
    open_dialog(page, "edit-purpose")
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
    form = open_dialog(page, "new-episode")
    form.get_by_label(translate("Title")).fill("Pilot")
    form.get_by_label(translate("Summary")).fill("Why Mondays hurt.")
    form.get_by_role("button", name=translate("Add episode")).click()
    flash(page, translate("Episode created."))
    heading(page, "#1 · Pilot")
    evidence.capture("episode-created")
    page.get_by_label(translate("Status")).select_option("script")
    page.get_by_role("button", name=translate("Save changes")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator(".steps [aria-current='step']")).to_have_text(translate("Script"))
    evidence.capture("episode-saved")
    move_on_board(page, translate, evidence)
    research_episode(page, translate, evidence)


def move_on_board(page: Page, translate: Translator, evidence: Evidence) -> None:
    page.locator(".back-link a").click()
    script = page.locator(".board-column[data-stage='script']")
    expect(script).to_contain_text("#1 · Pilot")
    evidence.capture("episodes-board")
    stage = page.get_by_label(translate("Stage of episode %(number)s", number=1))
    stage.select_option("production")
    flash(page, translate("Episode moved."))
    expect(page.locator(".board-column[data-stage='production']")).to_contain_text("#1 · Pilot")
    page.get_by_role("link", name="#1 · Pilot").click()
    heading(page, "#1 · Pilot")


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
    source = open_dialog(page, "new-source")
    source.get_by_label(translate("Link")).fill("https://example.com/mondays")
    source.get_by_label(translate("Title")).fill(EPISODE_SOURCE)
    source.get_by_label(translate("What it supports")).fill("Mondays feel longer.")
    source.get_by_role("button", name=translate("Add source")).click()
    flash(page, translate("Source added."))
    expect(page.locator("#sources")).to_contain_text(EPISODE_SOURCE)
    evidence.capture("research-source")
    link_pitch(page, translate, evidence)


def link_pitch(page: Page, translate: Translator, evidence: Evidence) -> None:
    """Create a pitch with a source, then build the episode on it and see its research there."""
    research_url = page.url
    section(page, translate, "Pitches")
    evidence.capture("pitches-empty")
    create_pitch(page, translate, PITCH_TITLE, (PITCH_SOURCE, "https://example.com/sleep"))
    evidence.capture("pitch")
    section(page, translate, "Pitches")
    create_pitch(
        page, translate, SECOND_PITCH_TITLE, (SECOND_PITCH_SOURCE, "https://example.com/scaries")
    )
    page.goto(research_url)
    chosen = page.locator("#pitch")
    # The first pitch is the main one; a second picker appears once the first is saved.
    chosen.get_by_label(translate("Main pitch")).select_option(label=PITCH_TITLE)
    chosen.get_by_role("button", name=translate("Save pitches")).click()
    flash(page, translate("Changes saved."))
    chosen = page.locator("#pitch")
    chosen.get_by_label(translate("Pitch %(position)s", position=2)).select_option(
        label=SECOND_PITCH_TITLE
    )
    chosen.get_by_role("button", name=translate("Save pitches")).click()
    flash(page, translate("Changes saved."))
    # Each pitch's research shows on the episode as it is on the pitch, never copied, in order.
    first, second = page.locator("#pitch-research-1-title"), page.locator("#pitch-research-2-title")
    expect(first).to_have_text(PITCH_TITLE)
    expect(second).to_have_text(SECOND_PITCH_TITLE)
    expect(page.locator("#pitch")).to_contain_text(PITCH_SOURCE)
    expect(page.locator("#pitch")).to_contain_text(SECOND_PITCH_SOURCE)
    evidence.capture("research-pitches")
    quick_search(page, translate, evidence)
    write_screenplay(page, translate, evidence)


def create_pitch(page: Page, translate: Translator, title: str, source: tuple[str, str]) -> None:
    """Create a pitch from the pitches page and give it one source."""
    form = open_dialog(page, "new-pitch")
    form.get_by_label(translate("Title")).fill(title)
    form.get_by_label(translate("Summary")).fill(f"About {title.lower()}.")
    form.get_by_role("button", name=translate("Add pitch")).click()
    flash(page, translate("Pitch created."))
    heading(page, title)
    dialog = open_dialog(page, "new-source")
    dialog.get_by_label(translate("Link")).fill(source[1])
    dialog.get_by_label(translate("Title")).fill(source[0])
    dialog.get_by_label(translate("What it supports")).fill("Backs the pitch.")
    dialog.get_by_role("button", name=translate("Add source")).click()
    flash(page, translate("Source added."))
    expect(page.locator("#sources")).to_contain_text(source[0])


def quick_search(page: Page, translate: Translator, evidence: Evidence) -> None:
    """Find the episode with Ctrl+K and open it from the results."""
    page.keyboard.press("Control+k")
    dialog = page.locator("dialog#search")
    expect(dialog).to_be_visible()
    dialog.get_by_role("searchbox").fill("pilot")
    result = dialog.locator(".search-result", has_text="#1 · Pilot")
    expect(result).to_be_visible()
    evidence.capture("search")
    page.keyboard.press("ArrowDown")
    expect(dialog.locator(".search-result").first).to_be_focused()
    result.click()
    heading(page, "#1 · Pilot")


def write_screenplay(page: Page, translate: Translator, evidence: Evidence) -> None:
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Screenplay"), exact=True).click()
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
    relate_characters(page, translate, evidence)
    pick_style(page, translate, evidence)
    record_claim(page, translate, evidence)
    record_delivery(page, translate, evidence)
    use_third_party_media(page, translate, evidence)
    write_short_script(page, translate, evidence)
    add_beat(page, translate, evidence)
    prepare_for_srstudio(page, translate, evidence)
    export_episode(page, translate, evidence)
    classify_episode(page, translate, evidence)
    publish_with_gaps(page, translate, evidence)


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


def relate_characters(page: Page, translate: Translator, evidence: Evidence) -> None:
    dialog = open_dialog(page, "add-character")
    dialog.locator("#new-character-name").fill("Bia")
    dialog.get_by_role("button", name=translate("Add character")).click()
    flash(page, translate("Added to the library."))
    page.get_by_role("main").get_by_role("link", name=translate("Relationships")).click()
    heading(page, translate("Relationships"))
    evidence.capture("relationships-empty")
    dialog = open_dialog(page, "add-relationship")
    dialog.locator("#first").select_option(label="ANA")
    dialog.locator("#second").select_option(label="Bia")
    dialog.locator("#relation").fill("flatmates")
    dialog.get_by_role("button", name=translate("Add relationship")).click()
    flash(page, translate("Added to the library."))
    heading(page, "ANA & Bia")
    page.locator("#tension").fill("Who finished the coffee")
    page.get_by_role("button", name=translate("Save changes")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator("#tension")).to_have_value("Who finished the coffee")
    evidence.capture("relationship")


def open_style_sheet(page: Page, translate: Translator) -> None:
    """Open the srstudio sheet of the library's visual style."""
    section(page, translate, "Library")
    page.locator("#styles").get_by_role("link", name=translate("srstudio sheet")).click()
    heading(page, "Warm documentary")


def describe_style(page: Page, translate: Translator, evidence: Evidence) -> None:
    """Give the library style its palette, fonts, cut rhythm, smallest text and safe zone."""
    open_style_sheet(page, translate)
    evidence.capture("style-sheet-empty")
    for key, color in (("ink", "#1a1a1a"), ("white", "#f5efe6"), ("shade", "#8a7a6a")):
        page.locator(f"#palette_{key}").fill(color)
    page.locator("#palette_background").fill("#e8dcc8")
    page.locator("#fonts_heading_family").fill("Inter")
    page.locator("#fonts_heading_weight").select_option("700")
    page.locator("#fonts_body_family").fill("Lora")
    page.locator("#fonts_body_weight").select_option("400")
    page.locator("#cut_rhythm_shortest_shot_s").fill("0.4")
    page.locator("#cut_rhythm_average_shot_s").fill("1.5")
    page.locator("#min_text_px").fill("42")
    for edge, percent in (("top", "10"), ("right", "6"), ("bottom", "20"), ("left", "6")):
        page.locator(f"#safe_zone_{edge}").fill(percent)
    # A shortest shot longer than the average is refused next to its field, keeping the rest.
    page.locator("#cut_rhythm_shortest_shot_s").fill("3")
    page.get_by_role("button", name=translate("Save sheet")).click()
    expect(page.locator("#cut_rhythm_average_shot_s-error")).to_be_visible()
    expect(page.locator("#min_text_px")).to_have_value("42")
    evidence.capture("style-sheet-errors")
    page.locator("#cut_rhythm_shortest_shot_s").fill("0.4")
    page.get_by_role("button", name=translate("Save sheet")).click()
    flash(page, translate("Changes saved."))
    # The sheet comes back with every structured value as saved.
    expect(page.locator("#palette_background")).to_have_value("#e8dcc8")
    expect(page.locator("#fonts_heading_family")).to_have_value("Inter")
    expect(page.locator("#fonts_heading_weight")).to_have_value("700")
    expect(page.locator("#cut_rhythm_shortest_shot_s")).to_have_value("0.4")
    expect(page.locator("#min_text_px")).to_have_value("42")
    expect(page.locator("#safe_zone_bottom")).to_have_value(re.compile(r"^20(\.0)?$"))
    evidence.capture("style-sheet")


def pick_style(page: Page, translate: Translator, evidence: Evidence) -> None:
    describe_style(page, translate, evidence)
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


def record_claim(page: Page, translate: Translator, evidence: Evidence) -> None:
    """List a claim the screenplay states, back it with a source and check that source."""
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Claims"), exact=True).click()
    evidence.capture("claims-empty")
    dialog = open_dialog(page, "new-claim")
    dialog.locator("#new-claim-quote").fill(CLAIM_QUOTE)
    dialog.locator("#new-claim-place").fill("INT. KITCHEN - DAY")
    backing = dialog.get_by_role("group", name=translate("Backing sources"))
    backing.get_by_label(EPISODE_SOURCE).check()
    dialog.get_by_role("group", name=translate("Primary sources")).get_by_label(
        EPISODE_SOURCE
    ).check()
    evidence.capture("claims-dialog")
    dialog.get_by_role("button", name=translate("Add claim")).click()
    flash(page, translate("Claim added."))
    claims = page.locator("#claims")
    expect(claims).to_contain_text(CLAIM_QUOTE)
    expect(claims).to_contain_text(EPISODE_SOURCE)
    expect(claims).to_contain_text(translate("Primary"))
    # Until a primary source is checked the episode warns, without blocking anything.
    expect(page.locator(".alert")).to_contain_text(
        translate("1 claim has no checked primary source. This does not block the episode.")
    )
    evidence.capture("claims-unchecked")
    claims.get_by_role("button", name=translate("Mark as checked")).click()
    flash(page, translate("Changes saved."))
    claims = page.locator("#claims")
    expect(claims).to_contain_text(translate("Checked"))
    expect(claims).to_contain_text(translate("Backed"))
    expect(page.locator(".alert")).to_have_count(0)
    evidence.capture("claims")


def record_delivery(page: Page, translate: Translator, evidence: Evidence) -> None:
    """Describe the main cut and record where it was posted."""
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Delivery"), exact=True).click()
    evidence.capture("delivery-empty")
    dialog = open_dialog(page, "new-cut")
    dialog.locator("#new-cut-name").fill("main")
    dialog.locator("#new-cut-caption").fill("Mondays again?")
    dialog.locator("#new-cut-alt").fill("Ana frowns at a kitchen calendar.")
    dialog.locator("#new-cut-hashtags").fill("#funny monday")
    dialog.get_by_role("button", name=translate("Add cut")).click()
    flash(page, translate("Changes saved."))
    cuts = page.locator("#cuts")
    expect(cuts).to_contain_text("Mondays again?")
    expect(cuts).to_contain_text(translate("Alt text") + ": Ana frowns at a kitchen calendar.")
    expect(cuts).to_contain_text("#funny")
    expect(cuts).to_contain_text("#monday")
    post = open_dialog(page, "post-tiktok")
    post.locator("#tiktok-cut").select_option("main")
    post.locator("#tiktok-url").fill(POST_LINK)
    post.get_by_role("button", name=translate("Save post")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator("#posts").get_by_role("link", name=POST_LINK)).to_be_visible()
    # The cut has no video yet, which the page says without blocking anything.
    expect(page.locator(".alert")).to_contain_text(translate("The package is incomplete"))
    evidence.capture("delivery")


def publish_with_gaps(page: Page, translate: Translator, evidence: Evidence) -> None:
    """Publishing an episode whose delivery is incomplete works and says what is missing."""
    section(page, translate, "Episodes")
    page.get_by_role("link", name=re.compile("Pilot")).first.click()
    page.get_by_label(translate("Status")).select_option("published")
    page.get_by_role("button", name=translate("Save changes")).click()
    flash(
        page,
        translate(
            "Episode saved as published, but its delivery package is incomplete. "
            "Open Delivery to see what is missing."
        ),
    )
    expect(page.locator(".steps [aria-current='step']")).to_have_text(translate("Published"))
    evidence.capture("episode-published-with-gaps")


def use_third_party_media(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Third-party media")
    evidence.capture("third-party-empty")
    dialog = open_dialog(page, "new-item")
    dialog.locator("#new-title").fill("Mars landing animation")
    dialog.locator("#new-url").fill("https://svs.gsfc.nasa.gov/mars")
    dialog.locator("#new-creator").fill("NASA")
    dialog.locator("#new-rights").select_option("public_domain")
    dialog.locator("#new-attribution").fill("NASA/JPL-Caltech")
    dialog.get_by_role("button", name=translate("Add item")).click()
    flash(page, translate("Added to third-party media."))
    heading(page, "Mars landing animation")
    evidence.capture("third-party-item")
    add_3d_model(page, translate, evidence)
    section(page, translate, "Episodes")
    page.get_by_role("link", name=re.compile("Pilot")).first.click()
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Third-party")).click()
    use = open_dialog(page, "new-use")
    use.locator("#media_id").select_option(label="Mars landing animation")
    use.locator("#use-start").fill("0:05")
    use.locator("#use-end").fill("0:12")
    use.get_by_role("button", name=translate("Record use")).click()
    flash(page, translate("Use recorded."))
    expect(page.locator("#credits")).to_contain_text("NASA/JPL-Caltech")
    evidence.capture("episode-third-party")


def add_3d_model(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Third-party media")
    dialog = open_dialog(page, "new-item")
    dialog.locator("#new-kind").select_option("model_3d")
    dialog.locator("#new-title").fill("Orange cube")
    dialog.locator("#new-url").fill("https://example.com/models/cube")
    dialog.locator("#new-rights").select_option("open_license")
    dialog.locator("#new-attribution").fill("Cube by Bia, CC BY 4.0")
    dialog.get_by_role("button", name=translate("Add item")).click()
    flash(page, translate("Added to third-party media."))
    upload = open_dialog(page, "upload-copy")
    upload.locator("#copy-file").set_input_files(
        {"name": "cube.glb", "mimeType": "model/gltf-binary", "buffer": cube_glb()}
    )
    upload.get_by_role("button", name=translate("Upload")).click()
    # The viewer draws the cube with the vendored three.js, which the CSP lets load.
    viewer = page.locator("figure.model-viewer")
    # The container draws WebGL in software, so building the studio light can take seconds.
    expect(viewer).to_have_class(re.compile(r"\bis-ready\b"), timeout=VIEWER_TIMEOUT_MS)
    expect(viewer.get_by_role("status")).to_have_text(
        translate("Drag to turn, scroll or pinch to zoom.")
    )
    evidence.capture("third-party-3d-model")


def write_short_script(page: Page, translate: Translator, evidence: Evidence) -> None:
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Short script")).click()
    evidence.capture("short-script-empty")
    script = page.locator("form", has=page.locator("#block-0-speech"))
    # The voice settings hide behind their one-line summary until opened.
    script.locator(".summary-line > summary").click()
    script.locator("#duration_s").fill("3")
    script.locator("#voice_id").fill("EXAVITQu4vr4xnSDxMaL")
    script.locator("#block-0-speech").fill("Monday [pause:0.3] [emphasis]again[/emphasis]?")
    script.locator("#block-0-duration_s").fill("3")
    expect(script.locator("#block-0 [data-range]")).to_have_text("0.0–3.0 s")
    # The script saves itself a moment after typing stops.
    saved_at = translate("Saved at %(time)s").split("%")[0].strip()
    expect(script.locator(".autosave-status")).to_contain_text(saved_at)
    script.get_by_role("button", name=translate("Add text")).click()
    script = page.locator("form", has=page.locator("#text-0-text"))
    expect(script.locator("#block-0-speech")).to_have_value(
        "Monday [pause:0.3] [emphasis]again[/emphasis]?"
    )
    script.locator("#text-0-text").fill("Monday again")
    script.locator("#text-0-shown").select_option("throughout")
    script.get_by_role("button", name=translate("Save short script")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator("main")).to_contain_text(
        translate("The last spoken block should be the punch.")
    )
    expect(page.locator("#block-0-speech")).to_have_value(
        "Monday [pause:0.3] [emphasis]again[/emphasis]?"
    )
    evidence.capture("short-script")


def add_beat(page: Page, translate: Translator, evidence: Evidence) -> None:
    block = page.locator("#block-0")
    block.locator("details.more > summary", has_text=translate("Beats")).click()
    # The blank row adds a beat once it names a character; the first beat starts at 0.
    expect(block.locator("#block-0-beat-0-at_s")).to_have_value("0")
    block.locator("#block-0-beat-0-character").select_option(label="ANA")
    block.locator("#block-0-beat-0-eyes").select_option("shock")
    block.locator("#block-0-beat-0-mouth").select_option("o")
    evidence.capture("short-script-beat")
    page.get_by_role("button", name=translate("Save short script")).first.click()
    flash(page, translate("Changes saved."))
    beats = page.locator("#block-0 details.more > summary", has_text=translate("Beats"))
    expect(beats).to_contain_text("1")
    expect(page.locator("#block-0-beat-0-eyes")).to_have_value("shock")


def prepare_for_srstudio(page: Page, translate: Translator, evidence: Evidence) -> None:
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name="srstudio", exact=True).click()
    export = page.locator("#export")
    # Without ElevenLabs there is no voice-over, so the checklist sends people to generate one.
    expect(export).to_contain_text(
        translate("Generate the voice-over of the short script, one take per block.")
    )
    expect(export.get_by_role("link", name=translate("Download srstudio zip"))).to_have_count(0)
    expect(page.locator("#slug")).to_have_value("pilot")
    page.locator("#slug").fill("pilot-mondays")
    page.locator("#seed").fill("7")
    page.get_by_role("button", name=translate("Save changes")).click()
    flash(page, translate("Changes saved."))
    expect(page.locator("#slug")).to_have_value("pilot-mondays")
    evidence.capture("srstudio")


def pick_chip(dialog: Locator, name: str, label: str) -> None:
    """Click the chip with this label among the inputs that carry this name."""
    chips = dialog.locator(f"input[name='{name}'] + label")
    chips.filter(has_text=re.compile(rf"^\W*{re.escape(label)}$")).click()


def export_episode(page: Page, translate: Translator, evidence: Evidence) -> None:
    menu = page.locator("summary").get_by_text(translate("Export"), exact=True)
    menu.click()
    evidence.capture("export-menu")
    # Each format downloads the episode book, named after the channel and the book.
    with page.expect_download() as saved:
        page.get_by_role("link", name="PDF", exact=True).click()
    assert saved.value.suggested_filename.startswith("funny-memes-episode-book-")
    assert Path(saved.value.path()).read_bytes().startswith(b"%PDF-")
    # Choosing a format closes the menu, so it never covers the tabs behind it.
    expect(page.get_by_role("link", name="Excel", exact=True)).to_be_hidden()
    menu.click()
    with page.expect_download() as saved:
        page.get_by_role("link", name="Excel", exact=True).click()
    assert saved.value.suggested_filename.endswith(".xlsx")
    expect(page.get_by_role("link", name="Excel", exact=True)).to_be_hidden()


def classify_episode(page: Page, translate: Translator, evidence: Evidence) -> None:
    tabs = page.get_by_role("navigation", name=translate("Episode pages"))
    tabs.get_by_role("link", name=translate("Classification"), exact=True).click()
    evidence.capture("classification-empty")
    dialog = open_dialog(page, "edit-genre")
    pick_chip(dialog, "genres", translate("Comedy"))
    evidence.capture("classification-dialog")
    dialog.get_by_role("button", name=translate("Save"), exact=True).click()
    dialog = page.locator("dialog#edit-genre")
    expect(dialog.locator("#humor_devices-error")).to_have_text(
        translate("Comedy needs at least one humor device")
    )
    pick_chip(dialog, "humor_devices", translate("Deadpan"))
    dialog.get_by_role("button", name=translate("Save"), exact=True).click()
    flash(page, translate("Changes saved."))
    expect(page.locator("#group-genre")).to_contain_text(translate("Deadpan"))
    evidence.capture("classification")
    section(page, translate, "Profile")
    expect(page.locator("#group-genre")).to_contain_text(translate("Comedy") + " 100%")
    evidence.capture("profile")


def download_book(page: Page, translate: Translator, evidence: Evidence) -> None:
    section(page, translate, "Books")
    tabs = page.get_by_role("navigation", name=translate("Books"))
    tabs.get_by_role("link", name=translate("Library book")).click()
    evidence.capture("books")
    # The button shows it is working while the server builds the book, then says it is done.
    with page.expect_download() as saved:
        page.get_by_role("button", name=translate("Download")).click()
    assert saved.value.suggested_filename.startswith("funny-memes-library-book-")
    assert Path(saved.value.path()).read_bytes().startswith(b"%PDF-")
    expect(page.locator("[data-download-status]")).to_have_text(translate("Downloaded."))
    evidence.capture("books-downloaded")


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
    download_book(page, translate, evidence)
    invite_member(page, translate, evidence)
    create_token(page, translate, evidence)
    delete_channel(page, server, translate, evidence)
