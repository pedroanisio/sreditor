"""Book files: a book written as JSON, an Excel workbook or a PDF (rule project.export).

JSON keeps English field names, as the API does. Excel and PDF are for people, so they are in
the language the person picked. The PDF carries the channel's brand: its logo on the cover and
in the running header, its first color on the cover band and headings, and the library's
pictures. The renderer may read the app's fonts and those exact files of the channel, nothing
else, so a book never reaches the internet or another channel's files.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from itertools import groupby
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from weasyprint import CSS, HTML  # type: ignore[import-untyped]  # ships no type hints
from weasyprint.urls import URLFetcher, URLFetcherResponse  # type: ignore[import-untyped]

from sreditor.assets import AssetMissing, AssetStore, StoredFile
from sreditor.book_operations import (
    SECTION_KINDS,
    Book,
    BookKind,
    BookPart,
    BookSection,
    CreditsBookPart,
    EditorialBookPart,
    EntryBookPart,
    EpisodeBookPart,
    PitchBookPart,
    ProfileBookPart,
    RelationshipsBookPart,
    ThirdPartyBookPart,
)
from sreditor.channels import Asset
from sreditor.classification import Classification
from sreditor.context import ItemContext, Question
from sreditor.export_formats import MEDIA_TYPES, ExportFormat
from sreditor.i18n import N_, Translator
from sreditor.library import LibraryKind
from sreditor.srstudio import CharacterSheet, PlaceSheet, PropSheet, StyleSheet
from sreditor.web.book_labels import (
    BOOK_LABELS,
    FONT_ROLE_LABELS,
    PAIR_LABELS,
    SECTION_LABELS,
)
from sreditor.web.classification_labels import DIMENSION_LABELS
from sreditor.web.export_files import (
    FILE_SCHEME,
    FONTS,
    JOIN,
    STATIC,
    STYLESHEETS,
    ExportFile,
    Sheet,
    classification_rows,
    classification_sheet,
    environment,
    file_name,
    joined_values,
    workbook,
)
from sreditor.web.labels import EPISODE_STATUS_LABELS
from sreditor.web.labels import KIND_LABELS as GUIDELINE_LABELS
from sreditor.web.workspace.external_media import KIND_LABELS as MEDIA_KIND_LABELS
from sreditor.web.workspace.external_media import PURPOSE_LABELS, RIGHTS_LABELS
from sreditor.web.workspace.item_context import QUESTION_LABELS
from sreditor.web.workspace.pitches import PITCH_STATUS_LABELS
from sreditor.web.workspace.short_script import FUNCTION_LABELS
from sreditor.web.workspace.srstudio import FACT_KIND_LABELS, ROLE_LABELS, STATUS_LABELS, WORLDS
from sreditor.web.workspace.srstudio import (
    KIND_LABELS as CHARACTER_KIND_LABELS,
)

BOOK_TEMPLATE = "book.html"
# The text tokens that read on a brand color, chosen by its relative luminance (WCAG 2).
ON_DARK = "--color-on-accent"
ON_LIGHT = "--color-text"
LIGHT_LUMINANCE = 0.4
FACT_SEPARATOR = " · "

ENTRY_SECTIONS = {kind: section for section, kind in SECTION_KINDS.items()}
PART_SECTIONS: dict[str, BookSection] = {
    "pitch": BookSection.PITCHES,
    "episode": BookSection.EPISODES,
    "relationships": BookSection.RELATIONSHIPS,
    "third_party": BookSection.THIRD_PARTY,
    "credits": BookSection.CREDITS,
    "editorial": BookSection.EDITORIAL,
    "profile": BookSection.PROFILE,
}


def part_section(part: BookPart) -> BookSection:
    """The section a part prints under."""
    if isinstance(part, EntryBookPart):
        return ENTRY_SECTIONS[part.entry.kind]
    return PART_SECTIONS[part.type]


def book_stem(book: Book) -> str:
    """The start of a book's file name, such as funny-memes-library-book."""
    return f"{book.channel.slug}-{book.kind.value}-book"


# Texts shared by Excel and PDF


def context_pairs(context: ItemContext, translate: Translator) -> list[tuple[str, str]]:
    """The context's answered questions as (question, answer)."""
    return [
        (translate(QUESTION_LABELS[question]), getattr(context, question.value))
        for question in Question
        if getattr(context, question.value)
    ]


def context_text(context: ItemContext, translate: Translator) -> str:
    """The context in one cell: each answered question on its own line."""
    return "\n".join(f"{label}: {answer}" for label, answer in context_pairs(context, translate))


def character_pairs(sheet: CharacterSheet, translate: Translator) -> list[tuple[str, str]]:
    """A character sheet's main facts."""
    return [
        (translate("Status"), translate(STATUS_LABELS[sheet.status])),
        (
            translate("Kind"),
            translate(CHARACTER_KIND_LABELS[sheet.character_kind]) if sheet.character_kind else "",
        ),
        (translate("World"), WORLDS[sheet.world] if sheet.world else ""),
        (translate("Rig"), sheet.rig),
        (
            translate("Comic role"),
            translate(ROLE_LABELS[sheet.comic.role]) if sheet.comic.role else "",
        ),
        (translate("Look"), JOIN.join(filter(None, [sheet.look.palette, sheet.look.outfits]))),
        (translate("Never changes"), sheet.look.never_changes),
        (translate("Voice"), sheet.voice.voice_name),
        (translate("Job"), sheet.who.job),
        (translate("Wants"), sheet.who.wants),
        (translate("Flaw"), sheet.who.flaw),
        (translate("Habit"), sheet.who.habit),
        (translate("Never"), JOIN.join(sheet.never)),
    ]


def style_pairs(sheet: StyleSheet, translate: Translator) -> list[tuple[str, str]]:
    """A visual style's data that is filled in: palette, fonts, cut rhythm, smallest text, safe zone."""
    pairs: list[tuple[str, str]] = []
    if sheet.palette is not None:
        palette = sheet.palette
        colors = [palette.ink, palette.white, palette.shade, palette.background]
        pairs.append((translate("Palette"), JOIN.join(colors)))
    if sheet.fonts is not None:
        fonts = [
            f"{translate(FONT_ROLE_LABELS[role])}: {font.family} {font.weight}"
            for role, font in sheet.fonts
            if font is not None
        ]
        pairs.append((translate("Fonts"), "\n".join(fonts)))
    if sheet.cut_rhythm is not None:
        rhythm = sheet.cut_rhythm
        shots = translate(
            "Shortest shot %(shortest)s s, average %(average)s s",
            shortest=f"{rhythm.shortest_shot_s:g}",
            average=f"{rhythm.average_shot_s:g}",
        )
        pairs.append((translate("Cut rhythm"), shots))
    if sheet.min_text_px is not None:
        pairs.append((translate("Smallest text"), f"{sheet.min_text_px} px"))
    if sheet.safe_zone is not None:
        zone = sheet.safe_zone
        edges = [zone.top, zone.right, zone.bottom, zone.left]
        pairs.append((translate("Safe zone"), " / ".join(f"{edge:g}%" for edge in edges)))
    return pairs


def sheet_pairs(part: EntryBookPart, translate: Translator) -> list[tuple[str, str]]:
    """A library entry's srstudio facts that are filled in, as (label, value)."""
    sheet = part.sheet.sheet
    pairs = [(translate("Slug"), part.sheet.slug)]
    if isinstance(sheet, CharacterSheet):
        pairs += character_pairs(sheet, translate)
    elif isinstance(sheet, PlaceSheet):
        pairs += [
            (translate("World"), WORLDS[sheet.world] if sheet.world else ""),
            (translate("Rig"), sheet.rig),
        ]
    elif isinstance(sheet, StyleSheet):
        pairs += [(translate("Renders with"), sheet.style.value if sheet.style else "")]
        pairs += style_pairs(sheet, translate)
    elif isinstance(sheet, PropSheet):
        pairs.append((translate("Rig"), sheet.rig))
    return [(label, value) for label, value in pairs if value]


def entry_facts(part: EntryBookPart) -> list[Any]:
    """The canon facts a character or place cites."""
    sheet = part.sheet.sheet
    if isinstance(sheet, CharacterSheet):
        return list(sheet.canon)
    if isinstance(sheet, PlaceSheet):
        return list(sheet.cites)
    return []


# Excel


def pitch_rows(book: Book, translate: Translator) -> list[list[str]]:
    """Each pitch with its research and context."""
    return [
        [
            part.pitch.title,
            translate(PITCH_STATUS_LABELS[part.pitch.status]),
            part.pitch.summary,
            part.notes,
            JOIN.join(f"#{number}" for number in part.episodes),
            context_text(part.context, translate),
        ]
        for part in book.of_type("pitch")
        if isinstance(part, PitchBookPart)
    ]


def episode_parts(book: Book) -> list[EpisodeBookPart]:
    """The book's episodes."""
    return [part for part in book.of_type("episode") if isinstance(part, EpisodeBookPart)]


def episode_rows(book: Book, translate: Translator) -> list[list[str]]:
    """Each episode with its own fields and its classification."""
    rows = []
    for part in episode_parts(book):
        export = part.export
        classification = export.classification.classification
        rows.append(
            [
                str(export.episode.number),
                export.episode.title,
                translate(EPISODE_STATUS_LABELS[export.episode.status]),
                export.episode.summary,
                ", ".join(part_of.pitch.title for part_of in export.pitches),
                export.style.name if export.style else "",
                export.research_notes,
                export.screenplay.text,
                context_text(part.context, translate),
                *(
                    joined_values(classification, dimension, translate)
                    for dimension in DIMENSION_LABELS
                ),
            ]
        )
    return rows


def source_rows(book: Book, translate: Translator) -> list[list[str]]:
    """The sources of each episode and each pitch, saying whose they are."""
    rows = []
    for part in episode_parts(book):
        where = f"{translate('Episode')} #{part.export.episode.number}"
        rows += [
            [where, item.title, str(item.url), item.claim, str(item.accessed_on)]
            for item in part.export.sources
        ]
    for pitch in book.of_type("pitch"):
        if isinstance(pitch, PitchBookPart):
            where = f"{translate('Pitch')}: {pitch.pitch.title}"
            rows += [
                [where, item.title, str(item.url), item.claim, str(item.accessed_on)]
                for item in pitch.sources
            ]
    return rows


def entry_parts(book: Book) -> list[EntryBookPart]:
    """The book's library entries."""
    return [part for part in book.of_type("library_entry") if isinstance(part, EntryBookPart)]


def entry_rows(book: Book, kind: LibraryKind, translate: Translator) -> list[list[str]]:
    """Each library entry of a kind with its sheet's facts and context."""
    return [
        [
            part.entry.name,
            part.entry.description,
            "\n".join(f"{label}: {value}" for label, value in sheet_pairs(part, translate)),
            context_text(part.context, translate),
            JOIN.join(image.title for image in part.images),
        ]
        for part in entry_parts(book)
        if part.entry.kind is kind
    ]


def fact_rows(book: Book, translate: Translator) -> list[list[str]]:
    """Each canon fact a character or place cites."""
    return [
        [
            part.entry.name,
            fact.fact,
            f"#{fact.episode}",
            translate(FACT_KIND_LABELS[fact.kind]),
            fact.quote,
        ]
        for part in entry_parts(book)
        for fact in entry_facts(part)
    ]


def relationship_rows(book: Book, translate: Translator) -> list[list[str]]:
    """Each relationship between two characters."""
    return [
        [
            row.first,
            row.second,
            row.relationship.relation,
            row.relationship.dynamic,
            row.relationship.tension,
            translate(PAIR_LABELS[row.relationship.status]),
        ]
        for part in book.of_type("relationships")
        if isinstance(part, RelationshipsBookPart)
        for row in part.relationships
    ]


def third_party_rows(book: Book, translate: Translator) -> list[list[str]]:
    """Each third-party item with its rights and the episodes that use it."""
    return [
        [
            part.item.title,
            translate(MEDIA_KIND_LABELS[part.item.kind]),
            part.item.creator,
            translate(RIGHTS_LABELS[part.item.rights]),
            part.item.license_name,
            part.item.attribution,
            str(part.item.url),
            JOIN.join(f"#{number}" for number in part.episodes),
        ]
        for part in book.of_type("third_party")
        if isinstance(part, ThirdPartyBookPart)
    ]


def credit_rows(book: Book) -> list[list[str]]:
    """Each credit an episode owes, and each rights gap it has."""
    rows = []
    for part in book.of_type("credits"):
        if isinstance(part, CreditsBookPart):
            number = f"#{part.episode.number}"
            rows += [
                [number, line.title, line.text, str(line.url)] for line in part.credits.credits
            ]
    return rows


def editorial_rows(book: Book, translate: Translator) -> list[list[str]]:
    """Each editorial guideline with its kind."""
    rows = []
    for part in book.of_type("editorial"):
        if isinstance(part, EditorialBookPart):
            editorial = part.editorial
            single = [editorial.purpose, editorial.voice, editorial.audience]
            guidelines = [*filter(None, single), *editorial.pillars, *editorial.avoid]
            rows += [[translate(GUIDELINE_LABELS[line.kind]), line.text] for line in guidelines]
    return rows


SheetRows = Callable[[Book, Translator], list[list[str]]]


def headers(translate: Translator, labels: Iterable[str]) -> list[str]:
    """Column headers in the person's language."""
    return [translate(label) for label in labels]


ENTRY_HEADERS = (N_("Name"), N_("Description"), N_("Sheet"), N_("Context"), N_("Pictures"))
EPISODE_HEADERS = (
    N_("Number"),
    N_("Title"),
    N_("Status"),
    N_("Summary"),
    N_("Pitch"),
    N_("Visual style"),
    N_("Research notes"),
    N_("Screenplay"),
    N_("Context"),
)


def library_sheets(book: Book, translate: Translator) -> list[Sheet]:
    """One sheet per library kind the book holds, then the canon facts."""
    sheets = []
    for kind, section in ENTRY_SECTIONS.items():
        rows = entry_rows(book, kind, translate)
        if rows:
            name = translate(SECTION_LABELS[section])
            sheets.append(Sheet(name=name, headers=headers(translate, ENTRY_HEADERS), rows=rows))
    facts = fact_rows(book, translate)
    if facts:
        labels = (N_("Name"), N_("Fact"), N_("Episode"), N_("How it shows"), N_("Quote"))
        sheets.append(
            Sheet(name=translate("Canon facts"), headers=headers(translate, labels), rows=facts)
        )
    return sheets


def plain_sheets(book: Book, translate: Translator) -> list[Sheet]:
    """The sheets of pitches, episodes, sources, relationships, rights, credits and editorial."""
    episode_headers = headers(translate, EPISODE_HEADERS)
    episode_headers += [translate(label) for label in DIMENSION_LABELS.values()]
    tables: list[tuple[str, list[str], list[list[str]]]] = [
        (
            translate("Editorial"),
            headers(translate, (N_("Kind"), N_("Text"))),
            editorial_rows(book, translate),
        ),
        (
            translate("Pitches"),
            headers(
                translate,
                (
                    N_("Title"),
                    N_("Status"),
                    N_("Summary"),
                    N_("Research notes"),
                    N_("Episodes"),
                    N_("Context"),
                ),
            ),
            pitch_rows(book, translate),
        ),
        (translate("Episodes"), episode_headers, episode_rows(book, translate)),
        (
            translate("Sources"),
            headers(
                translate, (N_("Where"), N_("Title"), N_("Link"), N_("Claim"), N_("Accessed on"))
            ),
            source_rows(book, translate),
        ),
        (
            translate("Relationships"),
            headers(
                translate,
                (
                    N_("Character"),
                    N_("Character"),
                    N_("Relation"),
                    N_("Dynamic"),
                    N_("Tension"),
                    N_("Status"),
                ),
            ),
            relationship_rows(book, translate),
        ),
        (
            translate("Third-party"),
            headers(
                translate,
                (
                    N_("Item"),
                    N_("Kind"),
                    N_("Creator"),
                    N_("Rights"),
                    N_("License"),
                    N_("Credit"),
                    N_("Link"),
                    N_("Episodes"),
                ),
            ),
            third_party_rows(book, translate),
        ),
        (
            translate("Credits"),
            headers(translate, (N_("Episode"), N_("Item"), N_("Credit"), N_("Link"))),
            credit_rows(book),
        ),
    ]
    return [Sheet(name=name, headers=columns, rows=rows) for name, columns, rows in tables if rows]


def book_sheets(book: Book, translate: Translator) -> list[Sheet]:
    """A book's workbook: a sheet per table it holds, or one that says it is empty."""
    sheets = plain_sheets(book, translate) + library_sheets(book, translate)
    for part in book.of_type("profile"):
        if isinstance(part, ProfileBookPart):
            profile = classification_sheet(part.declared.classification, translate)
            sheets.append(profile.model_copy(update={"name": translate("Profile")}))
    if not sheets:
        empty = [[translate("This book has nothing yet.")]]
        sheets.append(
            Sheet(name=translate(BOOK_LABELS[book.kind]), headers=[book.title], rows=empty)
        )
    return sheets


# PDF


class ChannelFilesOnly(URLFetcher):  # type: ignore[misc]
    """Lets the PDF renderer read the app's fonts and the book's own files, nothing else."""

    def __init__(self, allowed: frozenset[Path]) -> None:
        super().__init__()
        self.allowed = allowed

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> URLFetcherResponse:
        """The file, when it is a font or one of the book's files; ValueError for anything else."""
        parts = urlsplit(url)
        path = Path(unquote(parts.path)).resolve()
        is_known = path.is_relative_to(FONTS) or path in self.allowed
        if parts.scheme != FILE_SCHEME or not is_known:
            raise ValueError(
                f"PDF books read only the app's fonts and the channel's files, not {url}"
            )
        return super().fetch(url, headers)


def file_uri(assets: AssetStore, stored: Asset | StoredFile) -> Path | None:
    """The file behind a channel's asset, or None when it is gone."""
    try:
        return assets.path_of(stored)
    except AssetMissing:
        return None


def book_files(book: Book, assets: AssetStore) -> dict[str, Path]:
    """The logo and pictures the PDF shows, by key ("logo" or a third-party item's id)."""
    found: dict[str, Path | None] = {}
    if book.brand.logo is not None:
        found["logo"] = file_uri(assets, book.brand.logo)
    for part in entry_parts(book):
        for image in part.images:
            found[str(image.media_id)] = file_uri(assets, image.stored)
    return {key: path for key, path in found.items() if path is not None}


def grouped_parts(book: Book, translate: Translator) -> list[tuple[str, str, list[BookPart]]]:
    """The parts in runs of one section: (anchor, heading, parts)."""
    return [
        (section.value, translate(SECTION_LABELS[section]), list(parts))
        for section, parts in groupby(book.parts, key=part_section)
    ]


def label_maps(translate: Translator) -> dict[str, dict[Any, str]]:
    """The labels the part templates show, in the person's language."""
    maps: dict[str, dict[Any, Any]] = {
        "episode_status": EPISODE_STATUS_LABELS,
        "pitch_status": PITCH_STATUS_LABELS,
        "functions": FUNCTION_LABELS,
        "media_kinds": MEDIA_KIND_LABELS,
        "rights": RIGHTS_LABELS,
        "purposes": PURPOSE_LABELS,
        "guidelines": GUIDELINE_LABELS,
        "fact_kinds": FACT_KIND_LABELS,
        "pairs": PAIR_LABELS,
    }
    return {
        name: {key: translate(label) for key, label in labels.items()}
        for name, labels in maps.items()
    }


def brand_ink(color: str) -> str:
    """The text token that reads on a brand color: white on dark colors, ink on light ones."""
    red, green, blue = (int(color[index : index + 2], 16) / 255 for index in (1, 3, 5))
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    return ON_DARK if luminance < LIGHT_LUMINANCE else ON_LIGHT


def set_rows(data: Classification, translate: Translator) -> list[Any]:
    """The dimensions someone filled in; a book leaves the unknown ones out."""
    return [row for row in classification_rows(data, translate) if row[-1]]


def cover_texts(book: Book, translate: Translator) -> dict[str, str]:
    """The cover's eyebrow, title and lead: an item's book is titled after the item, the
    channel's books after what they are, with the sections they hold as the lead."""
    label = translate(BOOK_LABELS[book.kind])
    if book.kind in (BookKind.PITCH, BookKind.EPISODE):
        return {"eyebrow": label, "heading": book.title, "lead": ""}
    held = dict.fromkeys(part_section(part) for part in book.parts)
    lead = JOIN.join(translate(SECTION_LABELS[section]) for section in held)
    return {"eyebrow": "", "heading": label, "lead": lead}


def book_context(book: Book, translate: Translator, files: dict[str, Path]) -> dict[str, Any]:
    """What book.html renders."""
    brand_color = book.brand.colors[0].hex if book.brand.colors else ""
    return {
        "book": book,
        **cover_texts(book, translate),
        "book_label": translate(BOOK_LABELS[book.kind]),
        "is_sectioned": book.kind not in (BookKind.PITCH, BookKind.EPISODE),
        "sections": grouped_parts(book, translate),
        "labels": label_maps(translate),
        "files": {key: path.as_uri() for key, path in files.items()},
        "brand_color": brand_color,
        "on_brand": brand_ink(brand_color) if brand_color else ON_DARK,
        "exported_on": translate.day(book.exported_at.date()),
        "context_pairs": lambda context: context_pairs(context, translate),
        "sheet_pairs": lambda part: sheet_pairs(part, translate),
        "entry_facts": entry_facts,
        "classification": lambda data: set_rows(data, translate),
    }


def book_pdf(book: Book, translate: Translator, assets: AssetStore) -> bytes:
    """A book as a printed dossier with the channel's brand."""
    files = book_files(book, assets)
    template = environment(translate).get_template(BOOK_TEMPLATE)
    page = template.render(**book_context(book, translate, files), lang=translate.locale)
    fetcher = ChannelFilesOnly(frozenset(files.values()))
    stylesheets = [CSS(filename=str(sheet), url_fetcher=fetcher) for sheet in STYLESHEETS]
    document = HTML(string=page, base_url=str(STATIC), url_fetcher=fetcher)
    rendered: bytes = document.write_pdf(stylesheets=stylesheets)
    return rendered


def book_file(
    book: Book, export_format: ExportFormat, translate: Translator, assets: AssetStore
) -> ExportFile:
    """A book in one format."""
    if export_format is ExportFormat.XLSX:
        content = workbook(book_sheets(book, translate))
    elif export_format is ExportFormat.PDF:
        content = book_pdf(book, translate, assets)
    else:
        content = book.model_dump_json(indent=2).encode()
    return ExportFile(
        content=content,
        media_type=MEDIA_TYPES[export_format],
        filename=file_name(book_stem(book), book.exported_at, export_format),
    )
