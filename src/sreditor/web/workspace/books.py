"""The channel's books (rule project.export): one page that offers the channel, library and
rights books with their options, as forms that download from the API's book routes.

Pitch and episode books download from the Export menu on their own pages. The forms send GET
requests, so a book is a link anyone may bookmark or share inside the channel; workspace.js
fetches it to show the button working while the file is built.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from sreditor.book_operations import (
    CHANNEL_BOOK,
    KIND_SECTIONS,
    LIBRARY_BOOK,
    RIGHTS_BOOK,
    BookKind,
)
from sreditor.deps import ServicesDep
from sreditor.episodes import EpisodeStatus
from sreditor.export_formats import ExportFormat
from sreditor.i18n import LOCALE_NAMES, N_, Translator
from sreditor.web.book_labels import BOOK_LABELS, SECTION_LABELS
from sreditor.web.labels import EPISODE_STATUS_LABELS
from sreditor.web.routes import translator
from sreditor.web.workspace.shell import PageAccess, View, channel_page, channel_url

router = APIRouter()

SECTION = "books"
PAGE = "/app/channels/{slug}/books"
# The books this page offers, in the order its tabs show them.
OFFERED: dict[BookKind, str] = {
    BookKind.CHANNEL: CHANNEL_BOOK,
    BookKind.LIBRARY: LIBRARY_BOOK,
    BookKind.RIGHTS: RIGHTS_BOOK,
}
BOOK_HINTS: dict[BookKind, str] = {
    BookKind.CHANNEL: N_(
        "Everything the channel holds: editorial, profile, pitches, each episode in full, the "
        "library and third-party media."
    ),
    BookKind.LIBRARY: N_(
        "The series bible: characters, places, visual styles and props with their sheets, "
        "pictures and canon facts, then how the characters relate."
    ),
    BookKind.RIGHTS: N_(
        "What the channel owes: each third-party item with its rights and credit line, then "
        "each episode's credits and rights gaps."
    ),
}
# Books that hold episodes can keep only some of them.
EPISODE_BOOKS = frozenset({BookKind.CHANNEL, BookKind.RIGHTS})
FORMAT_LABELS: dict[ExportFormat, str] = {
    ExportFormat.PDF: N_("PDF, to read and print"),
    ExportFormat.XLSX: N_("Excel, one sheet per table"),
    ExportFormat.JSON: N_("JSON, as data"),
}


def chosen_kind(value: str) -> BookKind:
    """The book a tab asks for; the channel book when the address names none it knows."""
    try:
        kind = BookKind(value)
    except ValueError:
        return BookKind.CHANNEL
    return kind if kind in OFFERED else BookKind.CHANNEL


def book_form(kind: BookKind, slug: str, translate: Translator) -> dict[str, object]:
    """What the form of one book shows: where it downloads from and its options."""
    return {
        "kind": kind,
        "action": OFFERED[kind].format(slug=slug),
        "hint": translate(BOOK_HINTS[kind]),
        "sections": [
            (section.value, translate(SECTION_LABELS[section]), "")
            for section in KIND_SECTIONS[kind]
        ],
        "has_episodes": kind in EPISODE_BOOKS,
        "statuses": [
            (status.value, translate(EPISODE_STATUS_LABELS[status]), "") for status in EpisodeStatus
        ],
        "formats": [(found.value, translate(label)) for found, label in FORMAT_LABELS.items()],
        "languages": list(LOCALE_NAMES.items()),
    }


@router.get(PAGE)
def books(request: Request, app: ServicesDep, access: PageAccess, book: str = "") -> Response:
    """The channel's books, one tab per book, each with its options."""
    translate = translator(request)
    kind = chosen_kind(book)
    slug = access.channel.slug
    page = channel_url(slug) + "/books"
    tabs = [
        (f"{page}?book={offered.value}", translate(BOOK_LABELS[offered]), offered.value)
        for offered in OFFERED
    ]
    context = {
        "tabs": tabs,
        "current": kind.value,
        "form": book_form(kind, slug, translate),
        "locale": translate.locale,
    }
    return channel_page(
        request,
        app,
        access,
        View(template="workspace/books.html", section=SECTION, context=context),
    )
