"""The workspace frame: who may open a page, the channel's menu, and the note after a save.

Pages need a signed-in session; a visitor without one goes to sign in and comes back. A channel
the person is not a member of answers 404, as the API does, so channels stay private. Actions
the person's role does not allow are not shown, and their POST answers 403 (web.app.permissions).
After a change, the page redirects (so a reload does not repeat it) and the next page says what
happened, from a short-lived cookie that holds only a key of FLASH_TEXT (web.app.feedback).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from sreditor.channels import Channel
from sreditor.deps import ChannelAccess, Services, ServicesDep, access_for, request_locale
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.operations import list_channels
from sreditor.problems import ProblemError
from sreditor.repository import ChannelNotFound
from sreditor.users import User
from sreditor.web.forms import AFTER_SIGN_IN
from sreditor.web.labels import platform_name
from sreditor.web.routes import SECURITY_HEADERS, page, redirect, signed_in

APP_PATH = AFTER_SIGN_IN
FLASH_COOKIE = "sreditor_flash"
# Long enough for the redirect that follows a save; the next page deletes it.
FLASH_SECONDS = 60
# Channels listed in the header's switcher; the channel list page shows the rest.
SWITCHER_LIMIT = 20


class SignInRequired(Exception):
    """The page needs a signed-in session; `next_path` is where to come back to."""

    def __init__(self, next_path: str) -> None:
        super().__init__(next_path)
        self.next_path = next_path


class PageNotFound(Exception):
    """The channel does not exist, or the person is not a member."""


class PageForbidden(Exception):
    """The person's role does not allow this action."""


class Flash(StrEnum):
    """What the page after a change says happened."""

    SAVED = "saved"
    CREATED = "created"
    DELETED = "deleted"
    INVITED = "invited"
    REMOVED = "removed"
    REVOKED = "revoked"
    LEFT = "left"
    PASSWORD = "password"
    UPLOADED = "uploaded"
    EPISODE_CREATED = "episode_created"
    EPISODE_DELETED = "episode_deleted"
    EPISODE_MOVED = "episode_moved"
    SOURCE_ADDED = "source_added"
    LIBRARY_ADDED = "library_added"
    PITCH_CREATED = "pitch_created"
    PITCH_DELETED = "pitch_deleted"
    ITEM_ADDED = "item_added"
    ITEM_DELETED = "item_deleted"
    USE_ADDED = "use_added"
    VOICE_GENERATED = "voice_generated"
    VOICE_REUSED = "voice_reused"
    TIMINGS_APPLIED = "timings_applied"
    LINK_ADDED = "link_added"
    CLAIM_ADDED = "claim_added"
    CLAIMS_UNCHECKED = "claims_unchecked"


CHANGED_MEANWHILE = N_(
    "Someone changed this channel after you opened the page. Reload it to see their changes, "
    "then try again."
)
CHANNEL_LEVEL = N_("Channel")
REFUSED = N_("This change was not allowed. Reload the page and try again.")

FLASH_TEXT: dict[Flash, str] = {
    Flash.SAVED: N_("Changes saved."),
    Flash.CREATED: N_("Channel created. Add its accounts next."),
    Flash.DELETED: N_("Channel deleted."),
    Flash.INVITED: N_("Invitation sent."),
    Flash.REMOVED: N_("Removed."),
    Flash.REVOKED: N_("Revoked."),
    Flash.LEFT: N_("You left the channel."),
    Flash.PASSWORD: N_("Password changed. Your other sessions were signed out."),
    Flash.UPLOADED: N_("File uploaded."),
    Flash.EPISODE_CREATED: N_("Episode created."),
    Flash.EPISODE_MOVED: N_("Episode moved."),
    Flash.EPISODE_DELETED: N_("Episode deleted."),
    Flash.SOURCE_ADDED: N_("Source added."),
    Flash.LIBRARY_ADDED: N_("Added to the library."),
    Flash.PITCH_CREATED: N_("Pitch created."),
    Flash.PITCH_DELETED: N_("Pitch deleted."),
    Flash.ITEM_ADDED: N_("Added to third-party media."),
    Flash.ITEM_DELETED: N_("Removed from third-party media."),
    Flash.USE_ADDED: N_("Use recorded."),
    Flash.VOICE_GENERATED: N_("Voice-over generated and added to the episode's media."),
    Flash.VOICE_REUSED: N_("Nothing changed since the last take, so it was reused at no cost."),
    Flash.TIMINGS_APPLIED: N_("The blocks now use the times measured in the voice-over."),
    Flash.LINK_ADDED: N_("Link added."),
    Flash.CLAIM_ADDED: N_("Claim added."),
    Flash.CLAIMS_UNCHECKED: N_(
        "Changes saved, but some claims still have no checked primary source. See the Claims page."
    ),
}


class SectionGroup(StrEnum):
    """A heading of the channel's menu, which keeps related sections together."""

    CHANNEL = N_("Channel")
    PRODUCTION = N_("Production")
    TEAM = N_("Team")


class Section(BaseModel):
    """One entry of a channel's menu, shown to people whose role has `permission`."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    suffix: str
    permission: Permission
    group: SectionGroup


# The channel's menu. A new module adds one entry here and its own routes and templates.
SECTIONS: tuple[Section, ...] = (
    Section(
        key="overview",
        label=N_("Overview"),
        suffix="",
        permission=Permission.READ,
        group=SectionGroup.CHANNEL,
    ),
    Section(
        key="accounts",
        label=N_("Accounts"),
        suffix="/accounts",
        permission=Permission.READ,
        group=SectionGroup.CHANNEL,
    ),
    Section(
        key="brand",
        label=N_("Brand"),
        suffix="/brand",
        permission=Permission.READ,
        group=SectionGroup.CHANNEL,
    ),
    Section(
        key="editorial",
        label=N_("Editorial"),
        suffix="/editorial",
        permission=Permission.READ,
        group=SectionGroup.CHANNEL,
    ),
    Section(
        key="profile",
        label=N_("Profile"),
        suffix="/profile",
        permission=Permission.READ,
        group=SectionGroup.CHANNEL,
    ),
    Section(
        key="pitches",
        label=N_("Pitches"),
        suffix="/pitches",
        permission=Permission.READ,
        group=SectionGroup.PRODUCTION,
    ),
    Section(
        key="episodes",
        label=N_("Episodes"),
        suffix="/episodes",
        permission=Permission.READ,
        group=SectionGroup.PRODUCTION,
    ),
    Section(
        key="library",
        label=N_("Library"),
        suffix="/library",
        permission=Permission.READ,
        group=SectionGroup.PRODUCTION,
    ),
    Section(
        key="external-media",
        label=N_("Third-party media"),
        suffix="/external-media",
        permission=Permission.READ,
        group=SectionGroup.PRODUCTION,
    ),
    Section(
        key="books",
        label=N_("Books"),
        suffix="/books",
        permission=Permission.READ,
        group=SectionGroup.PRODUCTION,
    ),
    Section(
        key="members",
        label=N_("Members"),
        suffix="/members",
        permission=Permission.READ,
        group=SectionGroup.TEAM,
    ),
    Section(
        key="settings",
        label=N_("Settings"),
        suffix="/settings",
        permission=Permission.WRITE_CHANNEL,
        group=SectionGroup.TEAM,
    ),
)


class View(BaseModel):
    """What a channel page renders: its template, its menu entry and the template's values."""

    model_config = ConfigDict(frozen=True)

    template: str
    section: str
    status_code: int = 200
    context: dict[str, Any] = Field(default_factory=dict)


class Outcome(BaseModel):
    """How a form went: errors by field ("form" for the whole form) and the status to answer."""

    model_config = ConfigDict(frozen=True)

    errors: dict[str, str] = Field(default_factory=dict)
    status_code: int = status.HTTP_200_OK

    @classmethod
    def invalid(cls, errors: dict[str, str]) -> Outcome:
        """Fields that need fixing."""
        return cls(errors=errors, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    @classmethod
    def conflict(cls, errors: dict[str, str]) -> Outcome:
        """A change that clashes with the current state, such as a newer revision."""
        return cls(errors=errors, status_code=status.HTTP_409_CONFLICT)

    @classmethod
    def refused(cls, errors: dict[str, str], status_code: int) -> Outcome:
        """A change the rules refuse, answered with the status the API uses."""
        return cls(errors=errors, status_code=status_code)


def channel_url(slug: str, suffix: str = "") -> str:
    """The address of a channel page."""
    return f"{APP_PATH}/channels/{slug}{suffix}"


def page_user(request: Request, app: ServicesDep) -> User:
    """The signed-in person; without a session, go sign in and come back here."""
    user = signed_in(request, app)
    if user is None:
        here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise SignInRequired(here)
    return user


PageUser = Annotated[User, Depends(page_user)]


def page_access(slug: str, user: PageUser, app: ServicesDep) -> ChannelAccess:
    """The person's access to the channel in the path; 404 when they are not a member."""
    try:
        return access_for(app, slug, user, None)
    except ChannelNotFound as missing:
        raise PageNotFound(slug) from missing


PageAccess = Annotated[ChannelAccess, Depends(page_access)]


def require(access: ChannelAccess, permission: Permission) -> None:
    """Refuse the action unless the person's role allows it."""
    if not access.can(permission):
        raise PageForbidden(permission)


NavLink = tuple[str, str, str]
NavGroup = tuple[str, list[NavLink]]


def section_links(access: ChannelAccess, translate: Translator) -> list[NavGroup]:
    """The menu as (heading, [(href, label, key)]), with only the sections this person may open."""
    slug = access.channel.slug
    groups: dict[SectionGroup, list[NavLink]] = {}
    for section in SECTIONS:
        if access.can(section.permission):
            link = (channel_url(slug, section.suffix), translate(section.label), section.key)
            groups.setdefault(section.group, []).append(link)
    return [(translate(group), links) for group, links in groups.items()]


def switcher(app: Services, user: User) -> list[tuple[str, str]]:
    """(slug, name) of the channels in the header's switcher."""
    found = list_channels(app, user, None, SWITCHER_LIMIT)
    return [(channel.slug, channel.name) for channel in found.items]


def flash_text(request: Request) -> str | None:
    """The msgid of the note left by the previous change, if any."""
    try:
        return FLASH_TEXT[Flash(request.cookies.get(FLASH_COOKIE, ""))]
    except ValueError:
        return None


def workspace_page(
    request: Request, app: Services, user: User, template: str, **context: Any
) -> HTMLResponse:
    """Render a workspace template with the header's switcher and any note from a change."""
    response = page(
        request,
        app,
        template,
        context.pop("status_code", 200),
        user=user,
        flash=flash_text(request),
        switcher=switcher(app, user),
        **context,
    )
    if FLASH_COOKIE in request.cookies:
        response.delete_cookie(FLASH_COOKIE, path=APP_PATH)
    return response


def channel_page(
    request: Request, app: Services, access: ChannelAccess, view: View
) -> HTMLResponse:
    """Render a channel page inside the channel's menu."""
    return workspace_page(
        request,
        app,
        access.user,
        view.template,
        status_code=view.status_code,
        access=access,
        channel=access.channel,
        channel_link=channel_url(access.channel.slug),
        revision=access.revision,
        sections=section_links(access, Translator(request_locale(request))),
        section=view.section,
        can=lambda name: access.can(Permission(name)),
        **view.context,
    )


# A text part's form saves itself as people type (workspace.js): it marks the request with this
# header and expects no page back, only the new revision, so the next save sends it.
AUTOSAVE_HEADER = "sreditor-autosave"
REVISION_HEADER = "Sreditor-Revision"


def is_autosave(request: Request) -> bool:
    """Whether the request is a text part saving itself rather than a person pressing Save."""
    return AUTOSAVE_HEADER in request.headers


def autosaved(revision: int) -> Response:
    """The answer to an autosave that went through: no body, the part's new revision."""
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={REVISION_HEADER: str(revision), **SECURITY_HEADERS},
    )


def autosave_refused(outcome: Outcome) -> Response:
    """The answer to an autosave that did not go through; the page names why from the status."""
    return Response(status_code=outcome.status_code, headers=SECURITY_HEADERS)


def done(request: Request, app: Services, url: str, flash: Flash) -> Response:
    """Redirect after a change, leaving a note for the next page to show."""
    response = redirect(request, url)
    response.set_cookie(
        FLASH_COOKIE,
        flash.value,
        max_age=FLASH_SECONDS,
        path=APP_PATH,
        httponly=True,
        secure=app.settings.has_secure_cookies,
        samesite="lax",
    )
    return response


def sign_in_url(next_path: str) -> str:
    """The sign-in page that brings the person back to `next_path`."""
    return f"/login?next={quote(next_path, safe='/')}"


def level_tabs(channel: Channel, suffix: str, translate: Translator) -> list[tuple[str, str, str]]:
    """(href, label, key) for the channel's own level and each of its accounts."""
    base = channel_url(channel.slug, suffix)
    tabs = [(base, translate(CHANNEL_LEVEL), "")]
    for account in channel.accounts:
        key = account.platform.value
        tabs.append((f"{base}?platform={key}", platform_name(account.platform), key))
    return tabs


def refused(problem: ProblemError, translate: Translator) -> Outcome:
    """A service refusal shown above the form; its English detail stays in the API."""
    return Outcome.refused({"form": translate(REFUSED)}, problem.status)
