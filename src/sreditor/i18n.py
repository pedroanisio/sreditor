"""Translations for text meant for people: English is the base language, pt-BR the alternate.

Messages are their own English text (gettext style). `N_` only marks a string for the catalog
check in tests/test_i18n.py; a Translator looks the string up and fills %(name)s placeholders.
Rules: i18n.languages, i18n.catalog, i18n.negotiation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path

from sreditor.members import Role

BASE_LOCALE = "en"
# A list needs at least two items before "or" joins them.
FEWEST_TO_JOIN = 2
ALTERNATE_LOCALES = ("pt-BR",)
LOCALES = (BASE_LOCALE, *ALTERNATE_LOCALES)
LOCALE_NAMES = {"en": "English", "pt-BR": "Português (Brasil)"}
LOCALE_DIR = Path(__file__).parent / "locales"


def N_(message: str) -> str:
    """Mark a string for translation where it is defined; translate it where it is shown."""
    return message


ROLE_NAMES = {
    Role.OWNER: N_("owner"),
    Role.EDITOR: N_("editor"),
    Role.CONTRIBUTOR: N_("contributor"),
    Role.VIEWER: N_("viewer"),
}


# A day reads as "Nov 7, 2026" in English and "7 de nov. de 2026" in pt-BR, never as ISO.
DAY_FORMAT = N_("%(month)s %(day)s, %(year)s")
MONTH_NAMES = (
    N_("Jan"),
    N_("Feb"),
    N_("Mar"),
    N_("Apr"),
    N_("May"),
    N_("Jun"),
    N_("Jul"),
    N_("Aug"),
    N_("Sep"),
    N_("Oct"),
    N_("Nov"),
    N_("Dec"),
)


@cache
def catalog(locale: str) -> dict[str, str]:
    """English message -> translation. The base language needs no catalog."""
    if locale == BASE_LOCALE:
        return {}
    with (LOCALE_DIR / f"{locale}.json").open(encoding="utf-8") as handle:
        entries: dict[str, str] = json.load(handle)
    return entries


@dataclass(frozen=True)
class Translator:
    """Translates English msgids into one locale, filling %(name)s placeholders."""

    locale: str = BASE_LOCALE

    def __call__(self, message: str, **values: object) -> str:
        """The message in this locale, falling back to English, with placeholders filled."""
        text = catalog(self.locale).get(message, message)
        return text % values if values else text

    def role(self, role: Role) -> str:
        """A role's name in this locale."""
        return self(ROLE_NAMES[role])

    def day(self, value: date) -> str:
        """A calendar day as people read it in this locale, such as "Nov 7, 2026"."""
        month = self(MONTH_NAMES[value.month - 1])
        return self(DAY_FORMAT, month=month, day=value.day, year=value.year)

    def any_of(self, items: list[str]) -> str:
        """'a, b or c' in this locale."""
        if len(items) < FEWEST_TO_JOIN:
            return "".join(items)
        return self("%(items)s or %(last)s", items=", ".join(items[:-1]), last=items[-1])


def match_locale(tag: str | None) -> str | None:
    """The supported locale for a language tag such as 'pt', 'pt-PT' or 'en-US', if any."""
    if not tag:
        return None
    language = tag.strip().lower().split("-")[0]
    return {"en": "en", "pt": "pt-BR"}.get(language)


def negotiate(requested: str | None, remembered: str | None, accept_language: str) -> str:
    """The ?lang= switch, then the remembered choice, then Accept-Language, then English."""
    for choice in (requested, remembered):
        if locale := match_locale(choice):
            return locale
    weighted: list[tuple[float, str]] = []
    for part in accept_language.split(","):
        tag, _, quality = part.partition(";q=")
        try:
            weight = float(quality) if quality else 1.0
        except ValueError:
            continue
        weighted.append((weight, tag))
    for _weight, tag in sorted(weighted, key=lambda item: -item[0]):
        if locale := match_locale(tag):
            return locale
    return BASE_LOCALE
