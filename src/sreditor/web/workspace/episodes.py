"""Episode pages: the channel's episodes, a new one, and editing or deleting one."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.book_operations import EPISODE_BOOK
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_operations import (
    count_episode_parts,
    create_episode,
    delete_episode,
    get_episode,
    list_episodes,
    move_episode,
    replace_episode,
)
from sreditor.episodes import (
    SUMMARY_LENGTH,
    TITLE_LENGTH,
    Episode,
    EpisodeChanged,
    EpisodeInput,
    EpisodeNotFound,
    EpisodeStatus,
    RevisedEpisode,
)
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT, MAX_LIMIT
from sreditor.problems import ProblemError
from sreditor.web.forms import field_errors
from sreditor.web.labels import EPISODE_STATUS_LABELS, EPISODE_STATUS_TONES, episode_options
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.forms import EpisodeFields, MoveFields, RevisionFields, revision_of
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    channel_url,
    done,
    refused,
    require,
)

router = APIRouter()

EPISODES_SUFFIX = "/episodes"
UNKNOWN_STAGE = N_("Choose one of the stages on the board.")
EPISODE_CHANGED = N_(
    "Someone changed this episode after you opened the page. Reload it to see their changes, "
    "then try again."
)


def episodes_url(slug: str, number: int | None = None) -> str:
    """The episode list, or one episode's page."""
    suffix = EPISODES_SUFFIX if number is None else f"{EPISODES_SUFFIX}/{number}"
    return channel_url(slug, suffix)


DETAILS_TAB = N_("Details")
RESEARCH_TAB = N_("Research")
SCREENPLAY_TAB = N_("Screenplay")
SHORT_SCRIPT_TAB = N_("Short script")
MEDIA_TAB = N_("Media")
THIRD_PARTY_TAB = N_("Third-party")
CLASSIFICATION_TAB = N_("Classification")
WAIVERS_TAB = N_("Waivers")
SRSTUDIO_TAB = "srstudio"  # a product name, the same in every language
EPISODE_TABS = (
    ("", DETAILS_TAB, "details", Permission.READ),
    ("/research", RESEARCH_TAB, "research", Permission.READ),
    ("/screenplay", SCREENPLAY_TAB, "screenplay", Permission.READ),
    ("/short-script", SHORT_SCRIPT_TAB, "short-script", Permission.READ),
    ("/media", MEDIA_TAB, "media", Permission.READ),
    ("/external-media", THIRD_PARTY_TAB, "external-media", Permission.READ),
    ("/classification", CLASSIFICATION_TAB, "classification", Permission.READ),
    ("/waivers", WAIVERS_TAB, "waivers", Permission.READ_WAIVERS),
    ("/srstudio", SRSTUDIO_TAB, "srstudio", Permission.WRITE_CHANNEL),
)


def episode_tabs(
    access: ChannelAccess, number: int, translate: Translator
) -> list[tuple[str, str, str]]:
    """(href, label, key) for each page of one episode the person may open."""
    base = episodes_url(access.channel.slug, number)
    return [
        (base + suffix, translate(label), key)
        for suffix, label, key, needed in EPISODE_TABS
        if access.can(needed)
    ]


def episode_frame(
    app: Services, access: ChannelAccess, number: int, translate: Translator
) -> dict[str, Any]:
    """What the header shared by an episode's pages needs: its tabs, their counts and the stages."""
    counts = count_episode_parts(app, access, number)
    return {
        "episode_tabs": episode_tabs(access, number, translate),
        "tab_counts": {
            "research": counts.sources,
            "media": counts.media,
            "external-media": counts.uses,
            "waivers": counts.waivers,
        },
        "episode_steps": episode_options(translate),
        "export_link": EPISODE_BOOK.format(slug=access.channel.slug, number=number),
    }


def labels() -> dict[str, Any]:
    """What every episode template needs to name statuses and size its fields."""
    return {
        "episode_status_labels": EPISODE_STATUS_LABELS,
        "episode_status_tones": EPISODE_STATUS_TONES,
        "title_limit": TITLE_LENGTH,
        "summary_limit": SUMMARY_LENGTH,
    }


class ListMode(StrEnum):
    """How the episode list shows: a board with a column per stage, or a list by number."""

    BOARD = "board"
    LIST = "list"


BoardColumn = tuple[str, str, list[Episode]]


class ListView(BaseModel):
    """The episode list, with the new-episode form as it was sent."""

    model_config = ConfigDict(frozen=True)

    cursor: str = ""
    mode: ListMode = ListMode.BOARD
    outcome: Outcome = Outcome()
    sent: EpisodeFields | None = None


def board_columns(episodes: list[Episode], translate: Translator) -> list[BoardColumn]:
    """(stage, label, episodes) for each stage in order, empty stages included."""
    return [
        (stage, label, [row for row in episodes if row.status == stage])
        for stage, label in episode_options(translate)
    ]


def list_page(request: Request, app: Services, access: ChannelAccess, shown: ListView) -> Response:
    """The channel's episodes on a board or in a list and, for writers, the form to add one."""
    is_board = shown.mode is ListMode.BOARD
    limit = MAX_LIMIT if is_board else DEFAULT_LIMIT
    found = list_episodes(app, access, None if is_board else shown.cursor or None, limit)
    context = {
        "episodes": found.items,
        "next_cursor": found.next_cursor,
        "list_mode": shown.mode.value,
        "board": board_columns(found.items, translator(request)) if is_board else [],
        "stage_options": episode_options(translator(request)),
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        **labels(),
    }
    view = View(
        template="workspace/episodes.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/episodes")
def episodes(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    cursor: str = "",
    view: ListMode = ListMode.BOARD,
) -> Response:
    """The channel's episodes, on a board by stage or in a list by number."""
    return list_page(request, app, access, ListView(cursor=cursor, mode=view))


def as_input(fields: EpisodeFields) -> EpisodeInput:
    """The episode a form describes; a new episode starts as an idea."""
    status = fields.status or EpisodeStatus.IDEA.value
    return EpisodeInput.model_validate(
        {"title": fields.title, "summary": fields.summary, "status": status}
    )


@router.post("/app/channels/{slug}/episodes")
def new_episode(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    fields: Annotated[EpisodeFields, Form()],
) -> Response:
    """Add an episode with the channel's next number and open it."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    translate = translator(request)
    try:
        created = create_episode(app, access, as_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
        return list_page(request, app, access, ListView(outcome=outcome, sent=fields))
    url = episodes_url(access.channel.slug, created.episode.number)
    return done(request, app, url, Flash.EPISODE_CREATED)


def found_episode(app: Services, access: ChannelAccess, number: int) -> RevisedEpisode:
    """The episode in the path; 404 when the channel has none with that number."""
    try:
        return get_episode(app, access, number)
    except EpisodeNotFound as missing:
        raise PageNotFound(number) from missing


class EpisodeView(BaseModel):
    """One episode's page, with the values and errors of the form that was sent."""

    model_config = ConfigDict(frozen=True)

    number: int
    outcome: Outcome = Outcome()
    sent: EpisodeFields | None = None


def episode_page(
    request: Request, app: Services, access: ChannelAccess, shown: EpisodeView
) -> Response:
    """An episode's details, the form to change them and, for editors, deleting it."""
    current = found_episode(app, access, shown.number)
    context = {
        "episode": current.episode,
        "episode_revision": current.revision,
        "values": shown.sent or current.episode,
        "errors": shown.outcome.errors,
        "status_options": episode_options(translator(request)),
        "list_link": episodes_url(access.channel.slug),
        **episode_frame(app, access, shown.number, translator(request)),
        "episode_tab": "details",
        **labels(),
    }
    view = View(
        template="workspace/episode.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get("/app/channels/{slug}/episodes/{number}")
def episode(request: Request, app: ServicesDep, access: PageAccess, number: int) -> Response:
    """One episode."""
    return episode_page(request, app, access, EpisodeView(number=number))


def save_outcome(
    request: Request, app: Services, access: ChannelAccess, number: int, fields: EpisodeFields
) -> Outcome | None:
    """Save the form; the errors to show, or None when it is saved."""
    translate = translator(request)
    try:
        replace_episode(app, access, number, as_input(fields), revision_of(fields.revision))
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except EpisodeChanged:
        return Outcome.conflict({"form": translate(EPISODE_CHANGED)})
    except ProblemError as problem:
        return refused(problem, translate)
    return None


@router.post("/app/channels/{slug}/episodes/{number}")
def save_episode(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[EpisodeFields, Form()],
) -> Response:
    """Save an episode's title, summary and status."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    outcome = save_outcome(request, app, access, number, fields)
    if outcome is not None:
        shown = EpisodeView(number=number, outcome=outcome, sent=fields)
        return episode_page(request, app, access, shown)
    return done(request, app, episodes_url(access.channel.slug, number), Flash.SAVED)


@router.post("/app/channels/{slug}/episodes/{number}/delete")
def remove_episode(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Delete an episode."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    found_episode(app, access, number)
    try:
        delete_episode(app, access, number, revision_of(fields.revision))
    except EpisodeChanged:
        outcome = Outcome.conflict({"form": translator(request)(EPISODE_CHANGED)})
        return episode_page(request, app, access, EpisodeView(number=number, outcome=outcome))
    return done(request, app, episodes_url(access.channel.slug), Flash.EPISODE_DELETED)


@router.post("/app/channels/{slug}/episodes/{number}/move")
def move_to_stage(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[MoveFields, Form()],
) -> Response:
    """Move an episode to another stage from the board, and come back to the board."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    translate = translator(request)
    try:
        move_episode(app, access, number, EpisodeStatus(fields.status))
    except ValueError:
        outcome = Outcome.invalid({"form": translate(UNKNOWN_STAGE)})
        return list_page(request, app, access, ListView(outcome=outcome))
    except EpisodeChanged:
        outcome = Outcome.conflict({"form": translate(EPISODE_CHANGED)})
        return list_page(request, app, access, ListView(outcome=outcome))
    except ProblemError as problem:
        return list_page(request, app, access, ListView(outcome=refused(problem, translate)))
    return done(request, app, episodes_url(access.channel.slug), Flash.EPISODE_MOVED)
