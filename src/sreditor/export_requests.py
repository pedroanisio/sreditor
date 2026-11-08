"""What a signed link can export: an episode or a channel export, or one of the books (rule
project.export).

A link stores only this request, never the file. The file is built from the live data when the
link is opened, with the issuing token's own role, so it is as fresh and as private as the API
download it stands for. Exports and books only read, so every request needs the read permission,
and none of them holds waivers.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from sreditor import book_operations as books
from sreditor import export_operations as exporting
from sreditor import pitch_operations
from sreditor.book_operations import (
    Book,
    BookKind,
    BookLanguage,
    BookOptions,
    BookRequest,
    SectionNotOffered,
    book_stem,
)
from sreditor.deps import ChannelAccess, Services
from sreditor.episode_operations import episode_access
from sreditor.export_formats import ExportFormat
from sreditor.i18n import Translator
from sreditor.members import Permission
from sreditor.problems import ProblemError
from sreditor.web.books import book_file
from sreditor.web.export_files import ExportFile, channel_file, episode_file, file_name

SectionedKind = Literal[BookKind.LIBRARY, BookKind.CHANNEL, BookKind.RIGHTS]


class ExportScope(StrEnum):
    """What an export holds."""

    EPISODE = "episode"
    CHANNEL = "channel"
    PITCH_BOOK = "pitch_book"
    EPISODE_BOOK = "episode_book"
    LIBRARY_BOOK = "library_book"
    CHANNEL_BOOK = "channel_book"
    RIGHTS_BOOK = "rights_book"


# The scopes that need an episode number, and the one that needs a pitch.
EPISODE_SCOPES = frozenset({ExportScope.EPISODE, ExportScope.EPISODE_BOOK})
# The books whose sections and episodes the options narrow, as the API's section, status, from
# and to query parameters do.
SECTIONED_SCOPES: dict[ExportScope, SectionedKind] = {
    ExportScope.LIBRARY_BOOK: BookKind.LIBRARY,
    ExportScope.CHANNEL_BOOK: BookKind.CHANNEL,
    ExportScope.RIGHTS_BOOK: BookKind.RIGHTS,
}
BOOK_KINDS: dict[ExportScope, BookKind] = {
    ExportScope.PITCH_BOOK: BookKind.PITCH,
    ExportScope.EPISODE_BOOK: BookKind.EPISODE,
    **SECTIONED_SCOPES,
}


class ExportRequest(BaseModel):
    """One export or book: its scope, the episode or pitch it is about, and how it is written."""

    model_config = ConfigDict(frozen=True)

    scope: ExportScope = Field(
        description="episode: the episode's data as the API's export_episode; channel: the "
        "channel summary of export_channel; pitch_book, episode_book, library_book, "
        "channel_book or rights_book: the matching book."
    )
    number: PositiveInt | None = Field(
        default=None, description="The episode's number; episode and episode_book need it."
    )
    pitch_id: UUID | None = Field(
        default=None, description="An id from list_pitches; pitch_book needs it."
    )
    format: ExportFormat = Field(
        default=ExportFormat.JSON,
        description="json (everything, English field names, with format_version), xlsx (one "
        "sheet per table) or pdf (a readable dossier). Use json to read it as data.",
    )
    language: BookLanguage = Field(
        default=BookLanguage.ENGLISH,
        description="en or pt-BR, for xlsx and pdf; json keeps English field names.",
    )
    options: BookOptions = Field(
        default=BookOptions(),
        description="Only for library_book, channel_book and rights_book: `sections` keeps "
        "some sections, and `episodes` keeps episodes by `statuses` or number range "
        "(`first`, `last`). Empty means everything the book offers.",
    )


def invalid_options(detail: str) -> ProblemError:
    """A 422 problem for options the scope does not take."""
    return ProblemError(status.HTTP_422_UNPROCESSABLE_CONTENT, detail, "validation")


def check_options(request: ExportRequest) -> None:
    """Refuse sections or an episode filter, which only the sectioned books take."""
    if request.scope in SECTIONED_SCOPES:
        try:
            books.chosen_sections(SECTIONED_SCOPES[request.scope], request.options)
        except SectionNotOffered as foreign:
            raise invalid_options(f"{foreign}; remove it from options.sections") from foreign
    elif request.options != BookOptions():
        raise invalid_options(
            f"A {request.scope} export takes no options; leave options out, or use "
            "library_book, channel_book or rights_book to narrow by sections or episodes"
        )


def required[T](value: T | None, request: ExportRequest, name: str) -> T:
    """The value a scope cannot do without, or a 422 that names it."""
    if value is None:
        raise invalid_options(f"A {request.scope} export needs {name}; pass it in target.export")
    return value


def check_request(app: Services, access: ChannelAccess, request: ExportRequest) -> None:
    """Check the permission and that the episode or pitch exists and the options fit."""
    access.require(Permission.READ)
    if request.scope in EPISODE_SCOPES:
        episode_access(app, access, required(request.number, request, "number"), Permission.READ)
    if request.scope is ExportScope.PITCH_BOOK:
        pitch_operations.get_pitch(app, access, required(request.pitch_id, request, "pitch_id"))
    check_options(request)


def stem_of(slug: str, request: ExportRequest) -> str:
    """The start of the file's name, such as funny-memes-ep1 or funny-memes-library-book."""
    if request.scope is ExportScope.EPISODE:
        return f"{slug}-ep{request.number}"
    if request.scope is ExportScope.CHANNEL:
        return slug
    return book_stem(slug, BOOK_KINDS[request.scope])


def export_filename(slug: str, request: ExportRequest, moment: datetime) -> str:
    """The file's name as the API would give it, such as funny-memes-ep1-2026-11-08.json."""
    return file_name(stem_of(slug, request), moment, request.format)


def book_of(app: Services, access: ChannelAccess, request: ExportRequest) -> Book:
    """The book a book scope asks for."""
    if request.scope is ExportScope.PITCH_BOOK:
        return books.pitch_book(app, access, required(request.pitch_id, request, "pitch_id"))
    if request.scope is ExportScope.EPISODE_BOOK:
        return books.episode_book(app, access, required(request.number, request, "number"))
    sectioned = BookRequest(kind=SECTIONED_SCOPES[request.scope], options=request.options)
    return books.sectioned_book(app, access, sectioned)


def build_export(app: Services, access: ChannelAccess, request: ExportRequest) -> ExportFile:
    """The file the request asks for, built from the channel's data as it is now."""
    translate = Translator(request.language.value)
    if request.scope is ExportScope.EPISODE:
        number = required(request.number, request, "number")
        return episode_file(
            exporting.export_episode(app, access, number), request.format, translate
        )
    if request.scope is ExportScope.CHANNEL:
        return channel_file(exporting.export_channel(app, access), request.format, translate)
    book = book_of(app, access, request)
    return book_file(book, request.format, translate, app.assets)
