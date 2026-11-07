"""Book routes: download a ready-made book of a pitch, an episode, the library, the channel or
its rights and credits, as JSON, Excel or PDF (rule project.export)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, PositiveInt, ValidationError

from sreditor import book_operations as books
from sreditor.book_operations import (
    CHANNEL_BOOK,
    EPISODE_BOOK,
    LIBRARY_BOOK,
    PITCH_BOOK,
    RIGHTS_BOOK,
    BookKind,
    BookOptions,
    BookRequest,
    BookSection,
    EpisodeFilter,
    SectionNotOffered,
)
from sreditor.deps import Access, ServicesDep, request_locale
from sreditor.episode_routes import Number
from sreditor.episodes import EpisodeStatus
from sreditor.export_formats import ExportFormat
from sreditor.export_routes import FILE_RESPONSES, Format, attachment
from sreditor.i18n import BASE_LOCALE, Translator
from sreditor.pitch_routes import PitchId
from sreditor.problems import ProblemError
from sreditor.web.books import book_file

router = APIRouter(tags=["export"])


class BookLanguage(StrEnum):
    """The languages a book's Excel and PDF are written in."""

    ENGLISH = BASE_LOCALE
    PORTUGUESE = "pt-BR"


class BookOutput(BaseModel):
    """The file a book downloads as: its format and the language people read it in."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    export_format: ExportFormat
    translate: Translator


def book_output(
    export_format: Format,
    request: Request,
    language: Annotated[
        BookLanguage | None,
        Query(
            alias="lang",
            description="en or pt-BR, for xlsx and pdf; without it, the request's language.",
        ),
    ] = None,
) -> BookOutput:
    """The format and language the request asks for."""
    locale = language.value if language else request_locale(request)
    return BookOutput(export_format=export_format, translate=Translator(locale))


def invalid_options(field: str, message: str) -> ProblemError:
    """A 422 problem naming the option at fault."""
    return ProblemError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        message,
        "validation",
        errors=[{"location": ["query", field], "message": message, "type": "value_error"}],
    )


def book_options(
    sections: Annotated[
        list[BookSection],
        Query(
            alias="section",
            description="Repeat to keep only some sections; none means all the book offers.",
        ),
    ] = [],  # noqa: B006  # FastAPI copies query defaults per request
    statuses: Annotated[
        list[EpisodeStatus],
        Query(alias="status", description="Repeat to keep only episodes of these statuses."),
    ] = [],  # noqa: B006  # FastAPI copies query defaults per request
    first: Annotated[
        PositiveInt | None, Query(alias="from", description="The first episode number to keep.")
    ] = None,
    last: Annotated[
        PositiveInt | None, Query(alias="to", description="The last episode number to keep.")
    ] = None,
) -> BookOptions:
    """The sections and episodes a book holds; 422 when the range ends before it starts."""
    try:
        episodes = EpisodeFilter(statuses=frozenset(statuses), first=first, last=last)
    except ValidationError as invalid:
        raise invalid_options("to", "The last episode must not come before the first") from invalid
    return BookOptions(sections=tuple(sections), episodes=episodes)


Output = Annotated[BookOutput, Depends(book_output)]
Options = Annotated[BookOptions, Depends(book_options)]


class SectionedBook(BaseModel):
    """A library, channel or rights book request as the route received it."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    request: BookRequest
    output: BookOutput


def sectioned(found: SectionedBook, access: Access, app: ServicesDep) -> Response:
    """Build a sectioned book and send it as a download; 422 for a section it lacks."""
    try:
        book = books.sectioned_book(app, access, found.request)
    except SectionNotOffered as foreign:
        raise invalid_options("section", str(foreign)) from foreign
    output = found.output
    return attachment(book_file(book, output.export_format, output.translate, app.assets))


@router.get(
    PITCH_BOOK, summary="Download a pitch book", response_class=Response, responses=FILE_RESPONSES
)
def export_pitch_book(
    pitch_id: PitchId, output: Output, access: Access, app: ServicesDep
) -> Response:
    """Download one pitch as a book: its title, status and summary, its context (what, who,
    where, when, why, how), research notes, sources and the episodes it feeds. Use it to share
    or archive a story idea; for the episodes themselves use export_episode_book. Needs read.
    """
    book = books.pitch_book(app, access, pitch_id)
    return attachment(book_file(book, output.export_format, output.translate, app.assets))


@router.get(
    EPISODE_BOOK,
    summary="Download an episode book",
    response_class=Response,
    responses=FILE_RESPONSES,
)
def export_episode_book(
    number: Number, output: Output, access: Access, app: ServicesDep
) -> Response:
    """Download one episode as a book: everything export_episode holds (research, sources,
    pitch, screenplay, short script, classification, style, third-party uses and credits) plus
    its context and the characters and places its screenplay names. The PDF carries the
    channel's logo and colors. Waivers, media files and the srstudio zip are never included.
    Needs read.
    """
    book = books.episode_book(app, access, number)
    return attachment(book_file(book, output.export_format, output.translate, app.assets))


@router.get(
    LIBRARY_BOOK,
    summary="Download the library book",
    response_class=Response,
    responses=FILE_RESPONSES,
)
def export_library_book(
    output: Output, options: Options, access: Access, app: ServicesDep
) -> Response:
    """Download the channel's library as a book (a series bible): characters, places, visual
    styles and props with their srstudio sheets, context, canon facts and up to three pictures
    linked to each, then the relationships between characters. Narrow it with section
    (characters, places, styles, props, relationships). Needs read.
    """
    request = BookRequest(kind=BookKind.LIBRARY, options=options)
    return sectioned(SectionedBook(request=request, output=output), access, app)


@router.get(
    CHANNEL_BOOK,
    summary="Download the channel book",
    response_class=Response,
    responses=FILE_RESPONSES,
)
def export_channel_book(
    output: Output, options: Options, access: Access, app: ServicesDep
) -> Response:
    """Download the whole channel as a book: editorial, profile, pitches, every episode in full,
    the library and third-party media. Narrow it with section (editorial, profile, pitches,
    episodes, characters, places, styles, props, relationships, third_party) and the episodes
    with status, from and to. For one episode, use export_episode_book. Needs read.
    """
    request = BookRequest(kind=BookKind.CHANNEL, options=options)
    return sectioned(SectionedBook(request=request, output=output), access, app)


@router.get(
    RIGHTS_BOOK,
    summary="Download the rights and credits book",
    response_class=Response,
    responses=FILE_RESPONSES,
)
def export_rights_book(
    output: Output, options: Options, access: Access, app: ServicesDep
) -> Response:
    """Download what the channel owes for third-party media: each item with its rights,
    license, credit line and the episodes that use it, then each episode's credits and rights
    gaps. Narrow it with section (third_party, credits) and the episodes with status, from and
    to. Needs read.
    """
    request = BookRequest(kind=BookKind.RIGHTS, options=options)
    return sectioned(SectionedBook(request=request, output=output), access, app)
