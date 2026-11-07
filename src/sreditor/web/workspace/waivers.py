"""An episode's waivers page: the signed releases behind it (rule project.waivers).

Waivers hold personal data, so the page, its forms and its downloads are only for people whose
role may read waivers (owners and editors); the tab is hidden from everyone else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.assets import BYTES_PER_MEGABYTE, AssetRejected
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT
from sreditor.waiver_operations import (
    WaiverChange,
    add_waiver,
    get_waiver,
    list_waivers,
    remove_waiver,
    update_waiver,
)
from sreditor.waiver_routes import waiver_file
from sreditor.waivers import (
    COVERS_LENGTH,
    SIGNER_LENGTH,
    WAIVER_ACCEPT,
    WAIVER_FORMAT_NAMES,
    NewWaiver,
    WaiverInput,
    WaiverNotFound,
)
from sreditor.web.forms import field_errors
from sreditor.web.routes import SECURITY_HEADERS, rejected_form, translator
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import RevisionFields, WaiverFields, revision_of
from sreditor.web.workspace.media import upload_refusal
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

WAIVERS_SUFFIX = "/waivers"
PAGE = "/app/channels/{slug}/episodes/{number}/waivers"
WAIVER_GONE = N_("This waiver was changed or removed after you opened the page. Reload the page.")


def waivers_url(slug: str, number: int) -> str:
    """The waivers page of an episode."""
    return episodes_url(slug, number) + WAIVERS_SUFFIX


def waiver_id_of(value: str) -> UUID:
    """The id in a path; a malformed one is a waiver that does not exist."""
    try:
        return UUID(value)
    except ValueError as malformed:
        raise WaiverNotFound(value) from malformed


class WaiverPath(BaseModel):
    """The episode and waiver a form's address names."""

    model_config = ConfigDict(frozen=True)

    number: int
    waiver_id: str


def waiver_path(number: int, waiver_id: str) -> WaiverPath:
    """The episode and waiver in the address."""
    return WaiverPath(number=number, waiver_id=waiver_id)


# Path values in one parameter, so handlers keep to five (code.functions.args).
WaiverTarget = Annotated[WaiverPath, Depends(waiver_path)]


class WaiversView(BaseModel):
    """The waivers page, with the form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    number: int
    outcome: Outcome = Outcome()
    sent: WaiverFields | None = None
    is_new: bool = False  # the upload dialog was sent
    editing: str = ""  # the id of the waiver whose dialog was sent
    cursor: str = ""


def waivers_page(
    request: Request, app: Services, access: ChannelAccess, shown: WaiversView
) -> Response:
    """The episode's waivers, with download links and, for editors, the dialogs."""
    require(access, Permission.READ_WAIVERS)
    current = found_episode(app, access, shown.number)
    found = list_waivers(app, access, shown.number, shown.cursor or None, DEFAULT_LIMIT)
    context = {
        "episode": current.episode,
        "waivers": found.items,
        "next_cursor": found.next_cursor,
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        "is_new": shown.is_new,
        "editing": shown.editing,
        "waivers_link": waivers_url(access.channel.slug, shown.number),
        **episode_frame(app, access, shown.number, translator(request)),
        "episode_tab": "waivers",
        "signer_limit": SIGNER_LENGTH,
        "covers_limit": COVERS_LENGTH,
        "accept": WAIVER_ACCEPT,
        "formats": WAIVER_FORMAT_NAMES,
        "max_bytes": app.waivers.max_bytes,
        "max_megabytes": app.waivers.max_bytes // BYTES_PER_MEGABYTE,
        **labels(),
    }
    view = View(
        template="workspace/waivers.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PAGE)
def waivers(
    request: Request, app: ServicesDep, access: PageAccess, number: int, cursor: str = ""
) -> Response:
    """The signed waivers behind an episode."""
    return waivers_page(request, app, access, WaiversView(number=number, cursor=cursor))


def described(fields: WaiverFields) -> WaiverInput:
    """What a form says about a waiver; an empty date is no date."""
    return WaiverInput.model_validate(
        {"signer": fields.signer, "covers": fields.covers, "signed_on": fields.signed_on or None}
    )


def waiver_errors(action: Callable[[], object], translate: Translator) -> Outcome | None:
    """Run an upload or edit; the errors to show, or None when it worked."""
    try:
        action()
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except AssetRejected as rejection:
        return Outcome.invalid({"file": translate(rejection.message, **rejection.params)})
    except (WaiverNotFound, PartChanged):
        return Outcome.conflict({"form": translate(WAIVER_GONE)})
    return None


def change_of(number: int, waiver_id: str, fields: WaiverFields | RevisionFields) -> WaiverChange:
    """The waiver a form targets and the revision it was showing."""
    return WaiverChange(
        number=number,
        waiver_id=waiver_id_of(waiver_id),
        expected_revision=revision_of(fields.revision),
    )


@router.post(PAGE)
def upload(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[WaiverFields, Form()],
) -> Response:
    """Upload a signed waiver to the episode."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.READ_WAIVERS)
    require(access, Permission.WRITE_EDITORIAL)
    found_episode(app, access, number)

    def store() -> None:
        new = NewWaiver(channel=access.channel.slug, number=number, data=described(fields))
        assert fields.file is not None  # upload_refusal turned away a form without a file
        add_waiver(app, access, new, fields.file.file)

    outcome = upload_refusal(request, app, fields.file) or waiver_errors(store, translator(request))
    if outcome is None:
        return done(request, app, waivers_url(access.channel.slug, number), Flash.UPLOADED)
    shown = WaiversView(number=number, outcome=outcome, sent=fields, is_new=True)
    return waivers_page(request, app, access, shown)


@router.post(f"{PAGE}/{{waiver_id}}")
def save_waiver(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: WaiverTarget,
    fields: Annotated[WaiverFields, Form()],
) -> Response:
    """Change who signed a waiver, when, or what it covers."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    number = target.number
    require(access, Permission.WRITE_EDITORIAL)
    found_episode(app, access, number)

    def save() -> None:
        update_waiver(app, access, change_of(number, target.waiver_id, fields), described(fields))

    outcome = waiver_errors(save, translator(request))
    if outcome is None:
        return done(request, app, waivers_url(access.channel.slug, number), Flash.SAVED)
    shown = WaiversView(number=number, outcome=outcome, sent=fields, editing=target.waiver_id)
    return waivers_page(request, app, access, shown)


@router.post(f"{PAGE}/{{waiver_id}}/remove")
def drop_waiver(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: WaiverTarget,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Take a waiver out of the episode."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    number = target.number
    require(access, Permission.WRITE_EDITORIAL)
    found_episode(app, access, number)

    def remove() -> None:
        remove_waiver(app, access, change_of(number, target.waiver_id, fields))

    outcome = waiver_errors(remove, translator(request))
    if outcome is None:
        return done(request, app, waivers_url(access.channel.slug, number), Flash.REMOVED)
    return waivers_page(request, app, access, WaiversView(number=number, outcome=outcome))


@router.get(f"{PAGE}/{{waiver_id}}/file")
def download(app: ServicesDep, access: PageAccess, number: int, waiver_id: str) -> Response:
    """The signed file, as a download; only the episode's own waivers, only for editors."""
    require(access, Permission.READ_WAIVERS)
    found_episode(app, access, number)
    try:
        found = get_waiver(app, access, number, waiver_id_of(waiver_id))
        return waiver_file(app, found, SECURITY_HEADERS)
    except WaiverNotFound as missing:
        raise PageNotFound(waiver_id) from missing
