"""The design system rules: tokens only (ui.tokens), shared macros and /design (ui.design-system)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor.api import create_app
from sreditor.settings import AppSettings
from tests.conftest import RecordingMailer, fast_passwords

WEB = Path(__file__).resolve().parent.parent / "src" / "sreditor" / "web"
STYLE = (WEB / "static" / "style.css").read_text()
EXPORT_STYLE = (WEB / "static" / "export.css").read_text()
TOKENS = re.sub(r"/\*.*?\*/", "", (WEB / "static" / "tokens.css").read_text(), flags=re.DOTALL)
ORIGIN = "https://sreditor.test"

COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
TOKEN_USE = re.compile(r"var\(\s*(--[\w-]+)\s*\)")
TOKEN_DEFINITION = re.compile(r"(--[\w-]+)\s*:")
# Media queries cannot read custom properties, so a breakpoint is written as its value there and
# defined once in tokens.css as --breakpoint-*.
MEDIA_CONDITION = re.compile(r"@media\s*\([^)]*?([\d.]+rem)\)")
BREAKPOINT = re.compile(r"(--breakpoint-[\w-]+):\s*([\d.]+rem)")
# A color literal or a number with a unit; unitless numbers (0, 1fr, opacity 1) are not values.
RAW_VALUE = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch)\(|"
    r"-?\b\d*\.?\d+(?:px|r?em|ms|s|ch|vh|vw|%|deg|turn)\b"
)


def block(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector starts with `selector`."""
    start = css.index(selector)
    return css[css.index("{", start) + 1 : css.index("}", start)]


def test_style_css_values_are_only_tokens() -> None:
    without_tokens = TOKEN_USE.sub("", MEDIA_CONDITION.sub("", COMMENT.sub("", STYLE)))
    assert RAW_VALUE.findall(without_tokens) == []


def test_style_css_media_queries_use_breakpoint_tokens() -> None:
    breakpoints = dict(BREAKPOINT.findall(TOKENS)).values()
    conditions = MEDIA_CONDITION.findall(COMMENT.sub("", STYLE))
    assert conditions and set(conditions) <= set(breakpoints)


def test_style_css_token_references_are_all_defined() -> None:
    defined = set(TOKEN_DEFINITION.findall(TOKENS))
    assert set(TOKEN_USE.findall(STYLE)) - defined == set()


def test_tokens_in_dark_theme_redefine_every_light_color() -> None:
    light = set(TOKEN_DEFINITION.findall(block(TOKENS, ":root,")))
    dark = set(TOKEN_DEFINITION.findall(block(TOKENS, ".theme-dark")))
    system_dark = set(TOKEN_DEFINITION.findall(block(TOKENS, ":root:not(.theme-light)")))
    assert light == dark == system_dark


def test_tokens_motion_durations_are_at_most_300ms() -> None:
    # --duration-spin is the loading indicator's turn, the exception in rule ui.motion.
    durations = re.findall(r"--duration-(?!spin)[\w-]+:\s*(\d+)ms", block(TOKENS, ":root {"))
    assert durations and all(100 <= int(value) <= 300 for value in durations)


# Component classes that only _ui.html may write (rule ui.design-system).
COMPONENT_CLASSES = re.compile(
    r'class="(?:button|alert|card|field|tag|hero|features|facts|channels|tags|actions|'
    r"badge|menu|sidenav|tabs|panel|page-header|empty|items|confirm|chip|preview|mark|toast|dialog|"
    r"checklist|counter|copy|pager|choices|item-main|item-side|fields|meta|inherited|tiles|tile|numbered|mono|text-block|player)\b"
)
# WCAG AA: 4.5:1 for text, 3:1 for the outline of a control (1.4.3 and 1.4.11).
TEXT_CONTRAST = 4.5
CONTROL_CONTRAST = 3.0
TEXT_PAIRS = (
    ("text", "bg"),
    ("text", "surface"),
    ("text-muted", "bg"),
    ("text-muted", "surface"),
    ("accent", "bg"),
    ("accent", "surface"),
    ("on-accent", "accent"),
    ("on-accent", "accent-hover"),
    ("on-accent", "accent-pressed"),
    ("accent", "accent-soft"),
    ("danger", "danger-soft"),
    ("danger", "surface"),
    ("success", "success-soft"),
    ("warning", "warning-soft"),
    ("on-danger", "danger"),
    ("on-danger", "danger-hover"),
)
CONTROL_PAIRS = (("border-strong", "bg"), ("border-strong", "surface"), ("focus", "surface"))
THEME_SELECTORS = (":root,", ".theme-dark")
HEX_COLOR = re.compile(r"--color-([\w-]+):\s*(#[0-9a-fA-F]{6})")


def luminance(hex_color: str) -> float:
    """Relative luminance as WCAG defines it."""
    channels = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def low_contrast(pairs: tuple[tuple[str, str], ...], minimum: float) -> list[str]:
    """Every pair, in either theme, whose contrast is below `minimum`."""
    failures = []
    for selector in THEME_SELECTORS:
        colors = dict(HEX_COLOR.findall(block(TOKENS, selector)))
        for foreground, background in pairs:
            ratio = contrast(colors[foreground], colors[background])
            if ratio < minimum:
                failures.append(f"{selector} {foreground} on {background}: {ratio:.2f}")
    return failures


def test_tokens_text_pairs_meet_wcag_aa_contrast() -> None:
    assert low_contrast(TEXT_PAIRS, TEXT_CONTRAST) == []


def test_tokens_control_outlines_meet_wcag_aa_contrast() -> None:
    assert low_contrast(CONTROL_PAIRS, CONTROL_CONTRAST) == []


def test_tokens_defined_in_tokens_css_are_all_used() -> None:
    defined = set(TOKEN_DEFINITION.findall(TOKENS))
    used = set(TOKEN_USE.findall(STYLE)) | set(TOKEN_USE.findall(TOKENS))
    used |= set(TOKEN_USE.findall(EXPORT_STYLE))
    conditions = set(MEDIA_CONDITION.findall(COMMENT.sub("", STYLE)))
    used |= {name for name, value in BREAKPOINT.findall(TOKENS) if value in conditions}
    assert defined - used == set()


def test_templates_outside_ui_macros_use_no_component_classes() -> None:
    for path in (WEB / "templates").rglob("*.html"):
        if path.name != "_ui.html":
            assert COMPONENT_CLASSES.findall(path.read_text()) == [], path.name


@pytest.fixture
def dev_app(app_settings: AppSettings, mailer: RecordingMailer) -> FastAPI:
    settings = app_settings.model_copy(update={"has_dev_pages": True})
    return create_app(settings, mailer=mailer, passwords=fast_passwords())


def test_design_page_without_dev_pages_is_not_found(app: FastAPI) -> None:
    assert TestClient(app, base_url=ORIGIN).get("/design").status_code == 404


def test_design_page_with_dev_pages_shows_every_state(dev_app: FastAPI) -> None:
    response = TestClient(dev_app, base_url=ORIGIN).get("/design")
    assert response.status_code == 200
    for state in ("is-hover", "is-focus", "is-active", "is-loading"):
        assert f"button {state}" in response.text
    assert 'data-theme="light"' in response.text and 'data-theme="dark"' in response.text
    for component in (
        "badge warning",
        "menu start is-open",
        "menu up wide",
        "sidenav",
        "tabs",
        "confirm",
        "chip",
        "mark round",
        "toast success is-static",
        "dialog is-static",
        "icon-button is-active",
        "menu is-active",
        "tiles",
    ):
        assert f'class="{component}' in response.text
    assert "counter over" in response.text and 'class="button danger is-loading"' in response.text


def test_design_page_in_portuguese_is_translated(dev_app: FastAPI) -> None:
    response = TestClient(dev_app, base_url=ORIGIN).get("/design?lang=pt-BR")
    assert '<html lang="pt-BR">' in response.text and "Pressionado" in response.text
