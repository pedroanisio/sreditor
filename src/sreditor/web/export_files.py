"""Export files: the same export model written as JSON, an Excel workbook or a PDF dossier
(rule project.export).

JSON keeps English field names, as the API does; Excel and PDF are for people, so they are in
the person's language. Excel cells are always text, never formulas, so a note that starts with
"=" stays a note; the PDF renderer may read only the app's own fonts and nothing else.
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import xlsxwriter  # type: ignore[import-untyped]  # ships no type hints
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict
from weasyprint import CSS, HTML  # type: ignore[import-untyped]  # ships no type hints
from weasyprint.urls import URLFetcher, URLFetcherResponse  # type: ignore[import-untyped]

from sreditor.classification import Classification, Dimension, State
from sreditor.export_formats import MEDIA_TYPES, ExportFormat
from sreditor.export_operations import ChannelExport, EpisodeExport
from sreditor.field_types import MediaType
from sreditor.i18n import Translator
from sreditor.web.classification_labels import DIMENSION_LABELS, STATE_LABELS, VALUE_LABELS
from sreditor.web.labels import EPISODE_STATUS_LABELS
from sreditor.web.markdown_text import add_markdown_filters
from sreditor.web.workspace.external_media import PURPOSE_LABELS, RIGHTS_LABELS
from sreditor.web.workspace.short_script import FUNCTION_LABELS

HERE = Path(__file__).parent
STATIC = HERE / "static"
FONTS = STATIC / "fonts"
TEMPLATES = HERE / "templates" / "export"
STYLESHEETS = (STATIC / "tokens.css", STATIC / "export.css")
FILE_SCHEME = "file"
# Excel's limits: a cell holds at most 32,767 characters and a sheet name 31.
CELL_LIMIT = 32_767
SHEET_NAME_LIMIT = 31
COLUMN_WIDTH = 28
NAME_SAFE = re.compile(r"[^a-z0-9-]+")
JOIN = ", "


class ExportFile(BaseModel):
    """A file ready to download."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    media_type: MediaType
    filename: str


def file_name(stem: str, exported_at: datetime, export_format: ExportFormat) -> str:
    """A safe file name such as funny-memes-ep1-2026-11-07.pdf."""
    safe = NAME_SAFE.sub("-", stem.lower()).strip("-") or "export"
    return f"{safe}-{exported_at.date().isoformat()}.{export_format.value}"


# Excel


class Sheet(BaseModel):
    """One worksheet: its name, the column headers and the rows, all text."""

    model_config = ConfigDict(frozen=True)

    name: str
    headers: list[str]
    rows: list[list[str]]


def cell(text: str) -> str:
    """Text cut to what an Excel cell holds."""
    return text[:CELL_LIMIT]


def workbook(sheets: list[Sheet]) -> bytes:
    """An .xlsx file with one worksheet per sheet, its header row frozen and filterable."""
    buffer = io.BytesIO()
    # Text from people must never run as a formula or turn into a link or a number.
    options = {
        "in_memory": True,
        "strings_to_formulas": False,
        "strings_to_urls": False,
        "strings_to_numbers": False,
    }
    book = xlsxwriter.Workbook(buffer, options)
    bold = book.add_format({"bold": True})
    wrap = book.add_format({"text_wrap": True, "valign": "top"})
    for sheet in sheets:
        worksheet = book.add_worksheet(sheet.name[:SHEET_NAME_LIMIT])
        worksheet.set_column(0, max(len(sheet.headers) - 1, 0), COLUMN_WIDTH, wrap)
        for column, header in enumerate(sheet.headers):
            worksheet.write_string(0, column, cell(header), bold)
        for row_index, row in enumerate(sheet.rows, start=1):
            for column, value in enumerate(row):
                worksheet.write_string(row_index, column, cell(value))
        worksheet.freeze_panes(1, 0)
        worksheet.autofilter(0, 0, len(sheet.rows), max(len(sheet.headers) - 1, 0))
    book.close()
    return buffer.getvalue()


def values_text(data: Classification, dimension: Dimension, translate: Translator) -> list[str]:
    """A dimension as (state or main value, the other values) for a table."""
    found = data.of(dimension)
    if found.state is not State.PRESENT:
        return [translate(STATE_LABELS[found.state]), ""]
    values = [translate(VALUE_LABELS[value]) for value in data.values(dimension)]
    return [values[0], JOIN.join(values[1:])]


def classification_sheet(data: Classification, translate: Translator) -> Sheet:
    """Each dimension with its main value, or state, and the others."""
    rows = [
        [translate(DIMENSION_LABELS[dimension]), *values_text(data, dimension, translate)]
        for dimension in Dimension
    ]
    headers = [translate("Dimension"), translate("Main"), translate("Also")]
    return Sheet(name=translate("Classification"), headers=headers, rows=rows)


def episode_facts(export: EpisodeExport, translate: Translator) -> list[list[str]]:
    """The episode's own fields, one per row."""
    episode = export.episode
    return [
        [translate("Number"), str(episode.number)],
        [translate("Title"), episode.title],
        [translate("Summary"), episode.summary],
        [translate("Status"), translate(EPISODE_STATUS_LABELS[episode.status])],
        [translate("Pitch"), ", ".join(part.pitch.title for part in export.pitches)],
        [translate("Visual style"), export.style.name if export.style else ""],
        [translate("Research notes"), export.research_notes],
        [translate("Screenplay"), export.screenplay.text],
    ]


def source_rows(export: EpisodeExport, translate: Translator) -> list[list[str]]:
    """The episode's sources, then its pitch's, each saying where it comes from."""
    episode_sources = [
        [translate("Episode"), source.title, str(source.url), source.claim, str(source.accessed_on)]
        for source in export.sources
    ]
    pitch_sources = [
        [translate("Pitch"), source.title, str(source.url), source.claim, str(source.accessed_on)]
        for part in export.pitches
        for source in part.sources
    ]
    return episode_sources + pitch_sources


def third_party_rows(export: EpisodeExport, translate: Translator) -> list[list[str]]:
    """Each use of a third-party item, with what the item is and where it is used."""
    return [
        [
            found.item.title,
            found.item.creator,
            translate(RIGHTS_LABELS[found.item.rights]),
            translate(PURPOSE_LABELS[found.use.purpose]),
            found.use.scene,
            found.use.credit,
            str(found.item.url),
        ]
        for found in export.third_party
    ]


def episode_sheets(export: EpisodeExport, translate: Translator) -> list[Sheet]:
    """An episode's workbook: the episode, its classification, sources and third-party uses."""
    source_headers = [
        translate("Where"),
        translate("Title"),
        translate("Link"),
        translate("Claim"),
        translate("Accessed on"),
    ]
    use_headers = [
        translate("Item"),
        translate("Creator"),
        translate("Rights"),
        translate("Purpose"),
        translate("Scene"),
        translate("Credit"),
        translate("Link"),
    ]
    return [
        Sheet(
            name=translate("Episode"),
            headers=[translate("Field"), translate("Value")],
            rows=episode_facts(export, translate),
        ),
        classification_sheet(export.classification.classification, translate),
        Sheet(
            name=translate("Sources"),
            headers=source_headers,
            rows=source_rows(export, translate),
        ),
        Sheet(
            name=translate("Third-party"),
            headers=use_headers,
            rows=third_party_rows(export, translate),
        ),
    ]


def joined_values(data: Classification, dimension: Dimension, translate: Translator) -> str:
    """A dimension's values, main first, or its state, in one cell."""
    found = data.of(dimension)
    if found.state is not State.PRESENT:
        return translate(STATE_LABELS[found.state])
    return JOIN.join(translate(VALUE_LABELS[value]) for value in data.values(dimension))


def channel_sheets(export: ChannelExport, translate: Translator) -> list[Sheet]:
    """A channel's workbook: its episodes with their classification, and its profile."""
    dimensions = list(Dimension)
    episodes = [
        [
            str(row.episode.number),
            row.episode.title,
            translate(EPISODE_STATUS_LABELS[row.episode.status]),
            *(
                joined_values(row.classification.classification, dimension, translate)
                for dimension in dimensions
            ),
        ]
        for row in export.episodes
    ]
    headers = [translate("Number"), translate("Title"), translate("Status")]
    headers += [translate(DIMENSION_LABELS[dimension]) for dimension in dimensions]
    profile = classification_sheet(export.declared_profile.classification, translate)
    return [
        Sheet(name=translate("Episodes"), headers=headers, rows=episodes),
        profile.model_copy(update={"name": translate("Profile")}),
    ]


# PDF


class AppFontsOnly(URLFetcher):  # type: ignore[misc]
    """Lets the PDF renderer read the app's own fonts and refuses every other address."""

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> URLFetcherResponse:
        """The font file, when the address is one; ValueError for anything else."""
        parts = urlsplit(url)
        path = Path(unquote(parts.path)).resolve()
        if parts.scheme != FILE_SCHEME or not path.is_relative_to(FONTS):
            raise ValueError(f"PDF exports read only the app's fonts, not {url}")
        return super().fetch(url, headers)


def environment(translate: Translator) -> Environment:
    """Jinja for the export templates, with the person's language as _()."""
    jinja = Environment(
        loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(), auto_reload=False
    )
    jinja.globals["_"] = translate
    add_markdown_filters(jinja)
    return jinja


def pdf(template: str, context: dict[str, Any], translate: Translator) -> bytes:
    """A PDF rendered from an export template and the app's own stylesheets."""
    page = environment(translate).get_template(template).render(**context, lang=translate.locale)
    fetcher = AppFontsOnly()
    stylesheets = [CSS(filename=str(sheet), url_fetcher=fetcher) for sheet in STYLESHEETS]
    document = HTML(string=page, base_url=str(STATIC), url_fetcher=fetcher)
    rendered: bytes = document.write_pdf(stylesheets=stylesheets)
    return rendered


def classification_rows(data: Classification, translate: Translator) -> list[Any]:
    """Each dimension with its values for the PDF: (label, main or state, others, is set)."""
    rows = []
    for dimension in Dimension:
        main, others = values_text(data, dimension, translate)
        is_set = data.of(dimension).state is not State.UNKNOWN
        rows.append((translate(DIMENSION_LABELS[dimension]), main, others, is_set))
    return rows


def episode_pdf(export: EpisodeExport, translate: Translator) -> bytes:
    """An episode's dossier."""
    context = {
        "export": export,
        "status": translate(EPISODE_STATUS_LABELS[export.episode.status]),
        "classification": classification_rows(export.classification.classification, translate),
        "functions": {function: translate(label) for function, label in FUNCTION_LABELS.items()},
        "exported_on": translate.day(export.exported_at.date()),
    }
    return pdf("episode.html", context, translate)


def channel_pdf(export: ChannelExport, translate: Translator) -> bytes:
    """A channel's summary."""
    context = {
        "export": export,
        "status_labels": {
            status.value: translate(label) for status, label in EPISODE_STATUS_LABELS.items()
        },
        "profile": classification_rows(export.declared_profile.classification, translate),
        "exported_on": translate.day(export.exported_at.date()),
    }
    return pdf("channel.html", context, translate)


# Choosing


def episode_file(
    export: EpisodeExport, export_format: ExportFormat, translate: Translator
) -> ExportFile:
    """An episode's export in one format."""
    stem = f"{export.channel.slug}-ep{export.episode.number}"
    if export_format is ExportFormat.XLSX:
        content = workbook(episode_sheets(export, translate))
    elif export_format is ExportFormat.PDF:
        content = episode_pdf(export, translate)
    else:
        content = export.model_dump_json(indent=2).encode()
    return ExportFile(
        content=content,
        media_type=MEDIA_TYPES[export_format],
        filename=file_name(stem, export.exported_at, export_format),
    )


def channel_file(
    export: ChannelExport, export_format: ExportFormat, translate: Translator
) -> ExportFile:
    """A channel's export in one format."""
    if export_format is ExportFormat.XLSX:
        content = workbook(channel_sheets(export, translate))
    elif export_format is ExportFormat.PDF:
        content = channel_pdf(export, translate)
    else:
        content = export.model_dump_json(indent=2).encode()
    return ExportFile(
        content=content,
        media_type=MEDIA_TYPES[export_format],
        filename=file_name(export.channel.slug, export.exported_at, export_format),
    )
