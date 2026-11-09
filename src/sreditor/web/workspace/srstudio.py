"""The srstudio sheet of a library entry: a character's bible, a place's world, a style's look (rules project.srstudio.*).

The form flattens the sheet into plain fields (look_palette, voice_stability) and repeated rows
(canon_quote, open_question); sheet_data turns them back into the nested sheet the store keeps.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.datastructures import FormData

from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.episodes import EpisodeStatus
from sreditor.i18n import N_, Translator
from sreditor.library import EntryNotFound, LibraryEntry, LibraryKind
from sreditor.library_operations import EntryChange, get_entry
from sreditor.members import Permission
from sreditor.srstudio import (
    FONT_WEIGHT_STEP,
    MAX_FONT_WEIGHT,
    MIN_FONT_WEIGHT,
    TEXT_LENGTH,
    EntrySheet,
    SheetInput,
    SlugLocked,
    SlugTaken,
)
from sreditor.srstudio_operations import FactNotPublished, UnknownToonRig, get_sheet, save_sheet
from sreditor.srstudio_vocabulary import (
    CharacterKind,
    CharacterStatus,
    ComicRole,
    FactField,
    FactKind,
    Style,
    World,
)
from sreditor.web.forms import message_for
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.library import KIND_PATHS, entry_change, library_url
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    done,
    require,
)

router = APIRouter()

SHEET_SUFFIX = "/srstudio"
# Fields that hold one section of the nested sheet, by the prefix their form names share.
SECTIONS = ("look", "voice", "who", "comic", "palette")
# A visual style's data sections, named as the sheet names them; their form fields start with it
# (fonts_heading_family, cut_rhythm_shortest_shot_s, safe_zone_top).
STYLE_SECTIONS = ("fonts", "cut_rhythm", "safe_zone")
FONT_ROLES = ("heading", "body", "caption")
FONT_FIELDS = ("family", "weight")
# Repeated rows: the list they fill and the fields of each row.
FACT_COLUMNS = ("fact", "field", "episode", "kind", "quote")
# Selects always carry a value, so only these columns tell a filled row from the blank one.
CHOSEN_COLUMNS = frozenset({"field", "kind"})
ROWS = {
    "canon": FACT_COLUMNS,
    "cites": FACT_COLUMNS,
    "history": FACT_COLUMNS,
    "open": ("question", "proposal"),
}
KIND_ROWS = {LibraryKind.CHARACTER: ("canon", "open"), LibraryKind.LOCATION: ("cites",)}
SKIPPED = frozenset({"csrf_token", "revision", "slug"})
NEVER_FIELD = "never"
SPEAKER_BOOST = "voice_use_speaker_boost"
YES = "yes"
# Choices left empty mean "not decided"; the sheet keeps null for them.
OPTIONAL = frozenset({"world", "character_kind", "comic_role", "style", "min_text_px"})
SLUG_TAKEN = N_("Another entry of this kind already uses this slug.")
SLUG_LOCKED = N_("srstudio already knows this entry by its slug, so it can no longer change.")
NOT_PUBLISHED = N_("A fact can only cite a published episode.")
UNKNOWN_RIG = N_(
    "This toon rig is not in srstudio's cast; use one such as toon-dog, or leave it empty."
)
SHEET_CHANGED = N_("Someone changed this sheet after you opened it. Reload the page to see it.")
KIND_TITLES = {
    LibraryKind.CHARACTER: N_("Character"),
    LibraryKind.LOCATION: N_("Location"),
    LibraryKind.STYLE: N_("Visual style"),
    LibraryKind.PROP: N_("Prop"),
}
WORLDS = {
    World.OKAYVILLE: "Okayville",
    World.SAGA: "Saga",
    World.GLOSS: "Gloss",
    World.NEWS: "News",
    World.HOST: "Host",
    World.MIND: "Mind",
    World.MAPS: "Maps",
}
STATUS_LABELS = {CharacterStatus.CANON: N_("Canon"), CharacterStatus.PLACEHOLDER: N_("Placeholder")}
KIND_LABELS = {
    CharacterKind.PERSON: N_("Person"),
    CharacterKind.ANIMAL: N_("Animal"),
    CharacterKind.OBJECT: N_("Object"),
    CharacterKind.ARCHETYPE: N_("Archetype"),
}
ROLE_LABELS = {
    ComicRole.STRAIGHT: N_("Straight"),
    ComicRole.INSTIGATOR: N_("Instigator"),
    ComicRole.VICTIM: N_("Victim"),
    ComicRole.CHAOS: N_("Chaos"),
    ComicRole.KNOW_IT_ALL: N_("Know-it-all"),
}
FACT_KIND_LABELS = {
    FactKind.LINE: N_("A line"),
    FactKind.DRAWN: N_("Drawn"),
    FactKind.VOICE: N_("The voice"),
    FactKind.SEEN: N_("Seen"),
}


def sheet_url(slug: str, kind: LibraryKind, entry_id: UUID | str) -> str:
    """The page of one entry's srstudio sheet."""
    return f"{library_url(slug, kind)}/{entry_id}{SHEET_SUFFIX}"


def section_value(name: str, value: str) -> tuple[str, str, Any]:
    """(section, field, value) of a flattened section field such as voice_speed."""
    section, _, field = name.partition("_")
    if name == SPEAKER_BOOST:
        return section, field, value == YES
    return section, field, value


def style_section_of(name: str) -> str | None:
    """The style data section a form field belongs to, such as safe_zone for safe_zone_top."""
    return next((key for key in STYLE_SECTIONS if name.startswith(f"{key}_")), None)


def filled_values(section: dict[str, Any]) -> dict[str, Any]:
    """A section's fields someone filled in, so a half-filled section reports what is missing."""
    return {field: value for field, value in section.items() if str(value).strip()}


def font_roles(section: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """The fonts section as the sheet nests it: each role with its family and weight, or none."""
    roles: dict[str, dict[str, Any] | None] = {}
    for role in FONT_ROLES:
        fields = {field: section.get(f"{role}_{field}", "") for field in FONT_FIELDS}
        roles[role] = filled_values(fields) or None
    return roles


def style_sections(data: dict[str, Any]) -> None:
    """Nest the flat style data fields as the sheet keeps them; an empty section becomes null."""
    for key in STYLE_SECTIONS:
        if key not in data:
            continue
        if key == "fonts":
            roles = font_roles(data[key])
            data[key] = roles if any(roles.values()) else None
        else:
            data[key] = filled_values(data[key]) or None


def is_filled(row: dict[str, str]) -> bool:
    """Whether someone wrote in a row, rather than leaving the blank row that adds one."""
    return any(text.strip() for column, text in row.items() if column not in CHOSEN_COLUMNS)


def row_lists(kind: LibraryKind, form: FormData) -> dict[str, list[dict[str, str]]]:
    """A kind's repeated lists, leaving out rows whose fields are all empty (the blank row to add)."""
    return {prefix: filled_rows(form, prefix) for prefix in KIND_ROWS.get(kind, ())}


def filled_rows(form: FormData, prefix: str) -> list[dict[str, str]]:
    """One list's rows as posted (<prefix>_<field>, in order), without the blank row that adds one."""
    fields = ROWS[prefix]
    columns = [form.getlist(f"{prefix}_{field}") for field in fields]
    rows = [
        dict(zip(fields, map(str, values), strict=True)) for values in zip(*columns, strict=False)
    ]
    return [row for row in rows if is_filled(row)]


def sheet_data(kind: LibraryKind, form: FormData) -> dict[str, Any]:
    """The nested sheet a form describes, ready for validation."""
    data: dict[str, Any] = {"kind": kind}
    for name, raw in form.multi_items():
        value = str(raw)
        prefix = name.partition("_")[0]
        if name in SKIPPED or prefix in ROWS:
            continue
        if name == NEVER_FIELD:
            data[name] = [line.strip() for line in value.splitlines() if line.strip()]
        elif style_key := style_section_of(name):
            data.setdefault(style_key, {})[name.removeprefix(f"{style_key}_")] = value
        elif prefix in SECTIONS:
            section, field, typed = section_value(name, value)
            data.setdefault(section, {})[field] = typed
        else:
            data[name] = None if name in OPTIONAL and not value else value
    if kind is LibraryKind.CHARACTER and "comic" in data and not data["comic"].get("role"):
        data["comic"]["role"] = None
    if "palette" in data and not any(data["palette"].values()):
        data["palette"] = None
    style_sections(data)
    data.update(row_lists(kind, form))
    return data


def error_name(location: tuple[int | str, ...]) -> str:
    """The form field an error's location points at: look_palette, or canon_quote-2 for a row."""
    parts = [part for part in location if part not in {"sheet", *LibraryKind}]
    if len(parts) >= 3 and isinstance(parts[1], int):
        return f"{parts[0]}_{parts[2]}-{parts[1]}"
    return "_".join(str(part) for part in parts) or "form"


def sheet_errors(invalid: ValidationError, translate: Translator) -> dict[str, str]:
    """The first error for each form field."""
    errors: dict[str, str] = {}
    for error in invalid.errors():
        errors.setdefault(error_name(tuple(error["loc"])), message_for(dict(error), translate))
    return errors


class SheetView(BaseModel):
    """The sheet page, with the form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    kind: LibraryKind
    entry_id: UUID
    outcome: Outcome = Outcome()
    sent: dict[str, Any] | None = None


def option_lists(translate: Translator) -> dict[str, list[tuple[str, str]]]:
    """The choices of the sheet's selects, an empty first choice where the value may stay open."""
    undecided = [("", translate(N_("Not decided")))]
    return {
        "worlds": undecided + [(world.value, label) for world, label in WORLDS.items()],
        "statuses": [(status.value, translate(label)) for status, label in STATUS_LABELS.items()],
        "kinds": undecided
        + [(kind.value, translate(label)) for kind, label in KIND_LABELS.items()],
        "roles": undecided
        + [(role.value, translate(label)) for role, label in ROLE_LABELS.items()],
        "styles": undecided + [(style.value, style.value.capitalize()) for style in Style],
        "weights": undecided
        + [
            (str(weight), str(weight))
            for weight in range(MIN_FONT_WEIGHT, MAX_FONT_WEIGHT + 1, FONT_WEIGHT_STEP)
        ],
        "fact_fields": [(field.value, field.value) for field in FactField],
        "fact_kinds": [(kind.value, translate(label)) for kind, label in FACT_KIND_LABELS.items()],
        "booleans": [(YES, translate(N_("Yes"))), ("no", translate(N_("No")))],
    }


def published_options(app: Services, channel: str) -> list[tuple[str, str]]:
    """The channel's published episodes, the ones a fact may cite."""
    episodes = app.episodes.list(channel, None, app.episodes.count(channel) or 1)
    return [
        (str(episode.number), f"#{episode.number} · {episode.title}")
        for episode in episodes
        if episode.status is EpisodeStatus.PUBLISHED
    ]


def shown_sheet(stored: dict[str, Any], sent: dict[str, Any] | None) -> dict[str, Any]:
    """The sheet to show: what was sent over what is stored, so a section the form left out still renders."""
    shown = dict(stored)
    for key, value in (sent or {}).items():
        is_section = (
            isinstance(value, dict)
            and isinstance(shown.get(key), dict)
            and key not in STYLE_SECTIONS
        )
        shown[key] = {**shown[key], **value} if is_section else value
    return shown


def sheet_page(
    request: Request, app: Services, access: ChannelAccess, shown: SheetView
) -> Response:
    """The entry with its sheet, as a form for those who may write."""
    translate = translator(request)
    try:
        entry: LibraryEntry = get_entry(app, access, shown.kind, shown.entry_id)
    except EntryNotFound as missing:
        raise PageNotFound(str(shown.entry_id)) from missing
    stored: EntrySheet = get_sheet(app, access, shown.kind, shown.entry_id)
    context = {
        "entry": entry,
        "stored": stored,
        "sheet": shown_sheet(stored.sheet.model_dump(mode="json"), shown.sent),
        "slug_value": stored.slug
        if stored.exported_at
        else (shown.sent or {}).get("slug", stored.slug),
        "kind": shown.kind.value,
        "kind_title": KIND_TITLES[shown.kind],
        "errors": shown.outcome.errors,
        "action": sheet_url(access.channel.slug, shown.kind, entry.id),
        "library_link": library_url(access.channel.slug),
        "episodes": published_options(app, access.channel.slug),
        "text_limit": TEXT_LENGTH,
        **option_lists(translate),
    }
    view = View(
        template="workspace/srstudio_sheet.html",
        section="library",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


def entry_id_of(entry_id: str) -> UUID:
    """The entry an address names; 404 for a malformed id."""
    try:
        return UUID(entry_id)
    except ValueError as malformed:
        raise PageNotFound(entry_id) from malformed


def kind_at(kind_path: str) -> LibraryKind:
    """The kind an address names; 404 for any other segment."""
    if kind_path not in KIND_PATHS:
        raise PageNotFound(kind_path)
    return KIND_PATHS[kind_path]


@router.get("/app/channels/{slug}/library/{kind_path}/{entry_id}/srstudio")
def sheet(
    request: Request, app: ServicesDep, access: PageAccess, kind_path: str, entry_id: str
) -> Response:
    """A library entry's srstudio sheet."""
    shown = SheetView(kind=kind_at(kind_path), entry_id=entry_id_of(entry_id))
    return sheet_page(request, app, access, shown)


def save_errors(
    app: Services, access: ChannelAccess, change: EntryChange, form: FormData, translate: Translator
) -> Outcome | None:
    """Save the sheet a form describes; the errors to show, or None when it worked."""
    try:
        data = SheetInput.model_validate(
            {"slug": str(form.get("slug", "")), "sheet": sheet_data(change.kind, form)}
        )
        save_sheet(app, access, change, data)
    except ValidationError as invalid:
        return Outcome.invalid(sheet_errors(invalid, translate))
    except SlugTaken:
        return Outcome.conflict({"slug": translate(SLUG_TAKEN)})
    except SlugLocked:
        return Outcome.conflict({"slug": translate(SLUG_LOCKED)})
    except FactNotPublished:
        return Outcome.invalid({"form": translate(NOT_PUBLISHED)})
    except UnknownToonRig:
        return Outcome.invalid({"rig": translate(UNKNOWN_RIG)})
    except (PartChanged, EntryNotFound):
        return Outcome.conflict({"form": translate(SHEET_CHANGED)})
    return None


@router.post("/app/channels/{slug}/library/{kind_path}/{entry_id}/srstudio")
async def save(
    request: Request, app: ServicesDep, access: PageAccess, kind_path: str, entry_id: str
) -> Response:
    """Save a library entry's srstudio sheet."""
    form = await request.form()
    if rejected := rejected_form(request, app, str(form.get("csrf_token", ""))):
        return rejected
    kind = kind_at(kind_path)
    require(access, Permission.WRITE_CONTENT)
    change = entry_change(kind, str(entry_id_of(entry_id)), str(form.get("revision", "")))
    outcome = save_errors(app, access, change, form, translator(request))
    if outcome is None:
        return done(request, app, sheet_url(access.channel.slug, kind, entry_id), Flash.SAVED)
    sent = {"slug": str(form.get("slug", "")), **sheet_data(kind, form)}
    shown = SheetView(kind=kind, entry_id=change.entry_id, outcome=outcome, sent=sent)
    return sheet_page(request, app, access, shown)
