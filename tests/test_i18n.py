from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from jinja2 import Environment

from sreditor.i18n import (
    ALTERNATE_LOCALES,
    BASE_LOCALE,
    LOCALE_NAMES,
    LOCALES,
    Translator,
    catalog,
    match_locale,
    negotiate,
)
from sreditor.members import Role

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "sreditor"
TEMPLATES = PACKAGE / "web" / "templates"
# Calls whose first argument is a msgid: N_("..."), t("..."), self("...") in Translator,
# and self.translate("...") in Accounts.
TRANSLATING_NAMES = {"N_", "translate", "self"}
TRANSLATING_ATTRIBUTES = {"translate"}
PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def template_messages() -> set[str]:
    environment = Environment(extensions=["jinja2.ext.i18n"])
    found = set()
    for path in TEMPLATES.rglob("*.html"):
        for _line, _function, message in environment.extract_translations(path.read_text()):
            # A call with placeholders, _("Hi %(name)s", name=x), comes back as ("Hi %(name)s", None).
            first = message[0] if isinstance(message, tuple) else message
            if isinstance(first, str):
                found.add(first)
    return found


def is_translating(call: ast.Call) -> bool:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id in TRANSLATING_NAMES
    return isinstance(function, ast.Attribute) and function.attr in TRANSLATING_ATTRIBUTES


def python_messages() -> set[str]:
    found = set()
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and is_translating(node) and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
    return found


def all_messages() -> set[str]:
    return template_messages() | python_messages()


def test_extraction_from_templates_and_python_finds_both() -> None:
    assert "Run every channel from one place." in template_messages()
    assert "Wrong email or password." in python_messages()  # N_ in accounts.py
    assert "Verify your email" in python_messages()  # self.translate in accounts.py
    assert "This field is required." in python_messages()  # translate() in web/forms.py
    assert "%(items)s or %(last)s" in python_messages()  # self() in i18n.py


@pytest.mark.parametrize("locale", ALTERNATE_LOCALES)
def test_catalog_against_marked_strings_matches_both_ways(locale: str) -> None:
    """Rule i18n.catalog: every marked string is translated, and nothing else is."""
    entries = catalog(locale)
    messages = all_messages()
    assert sorted(messages - entries.keys()) == [], "untranslated"
    assert sorted(entries.keys() - messages) == [], "translations without a marked string"
    for message, translation in entries.items():
        assert translation.strip(), message
        assert set(PLACEHOLDER.findall(message)) == set(PLACEHOLDER.findall(translation)), message


def test_translator_in_english_returns_the_text() -> None:
    assert Translator()("Sign in") == "Sign in"


def test_translator_in_portuguese_uses_the_catalog() -> None:
    assert Translator("pt-BR")("Sign in") == "Entrar"


def test_translator_with_missing_entry_falls_back_to_english() -> None:
    assert Translator("pt-BR")("Not in the catalog") == "Not in the catalog"


def test_translator_with_placeholders_fills_them_after_translating() -> None:
    assert Translator("pt-BR")("Welcome, %(name)s", name="Ana") == "Boas-vindas, Ana"


@pytest.mark.parametrize(
    ("locale", "role", "name"),
    [
        ("en", Role.EDITOR, "editor"),
        ("pt-BR", Role.EDITOR, "editor"),
        ("pt-BR", Role.VIEWER, "leitor"),
    ],
)
def test_translator_role_in_each_language_names_the_role(
    locale: str, role: Role, name: str
) -> None:
    assert Translator(locale).role(role) == name


@pytest.mark.parametrize(
    ("locale", "items", "joined"),
    [
        ("en", ["a", "b", "c"], "a, b or c"),
        ("pt-BR", ["a", "b"], "a ou b"),
        ("en", ["a"], "a"),
        ("en", [], ""),
    ],
)
def test_translator_any_of_with_items_joins_them_with_or(
    locale: str, items: list[str], joined: str
) -> None:
    assert Translator(locale).any_of(items) == joined


def test_catalog_for_base_locale_is_empty() -> None:
    assert catalog(BASE_LOCALE) == {}


def test_locales_supported_are_english_and_portuguese() -> None:
    assert LOCALES == ("en", "pt-BR")
    assert set(LOCALE_NAMES) == set(LOCALES)


@pytest.mark.parametrize(
    ("tag", "locale"),
    [
        ("pt", "pt-BR"),
        ("pt-BR", "pt-BR"),
        ("pt-PT", "pt-BR"),
        ("PT-br", "pt-BR"),
        ("en-US", "en"),
        ("en", "en"),
        ("fr", None),
        ("", None),
        (None, None),
    ],
)
def test_match_locale_for_language_tag_returns_supported_locale(
    tag: str | None, locale: str | None
) -> None:
    assert match_locale(tag) == locale


@pytest.mark.parametrize(
    ("requested", "remembered", "accept", "locale"),
    [
        ("pt-BR", "en", "en", "pt-BR"),  # the switch wins
        (None, "pt-BR", "en", "pt-BR"),  # then the remembered choice
        ("xx", None, "pt-BR,pt;q=0.9,en;q=0.8", "pt-BR"),  # then Accept-Language
        (None, None, "fr-FR, en;q=0.5, pt;q=0.7", "pt-BR"),  # by weight
        (None, None, "fr, de;q=bad, en;q=0.1", "en"),
        (None, None, "fr, de", "en"),  # nothing supported: English
        (None, None, "", "en"),
    ],
)
def test_negotiate_with_switch_cookie_and_header_picks_in_that_order(
    requested: str | None, remembered: str | None, accept: str, locale: str
) -> None:
    assert negotiate(requested, remembered, accept) == locale
