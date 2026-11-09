"""An episode's srstudio page: its slug and seed, what the export still lacks, and the zip (rule project.srstudio).

Only owners and editors may export, so only they see the tab. The checklist runs the same checks
as the export without converting any audio, and links each missing piece to the page that fixes it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.srstudio import MAX_SEED, EpisodeSlugInput, SlugLocked, SlugTaken
from sreditor.srstudio_export import (
    NO_KIND,
    NO_STYLE,
    NO_WORLD,
    NOT_TOON,
    UNKNOWN_RIG,
    UNPUBLISHED,
    Missing,
    missing_pieces,
)
from sreditor.srstudio_operations import get_episode_slug, save_episode_slug
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import revision_of
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    View,
    channel_page,
    channel_url,
    done,
    require,
)

router = APIRouter()

SRSTUDIO_SUFFIX = "/srstudio"
PAGE = "/app/channels/{slug}/episodes/{number}/srstudio"
ZIP = "/channels/{slug}/episodes/{number}/srstudio.zip"
SLUG_TAKEN = N_("Another episode of this channel already uses this slug.")
SLUG_LOCKED = N_("srstudio already knows this episode by its slug, so it can no longer change.")
SLUG_CHANGED = N_("Someone changed the slug or seed after you opened the page. Reload the page.")
# Where each missing piece is fixed; anything about the script or its takes is fixed there.
LIBRARY_FIXES = frozenset({NO_WORLD, NO_KIND, NOT_TOON, UNKNOWN_RIG, UNPUBLISHED})
SHORT_SCRIPT_SUFFIX = "/short-script"
MEDIA_SUFFIX = "/media"
LIBRARY_SUFFIX = "/library"


class SlugFields(SubmittedFields):
    """The slug and seed form."""

    revision: str = ""
    slug: str = ""
    seed: str = ""


class SrstudioView(BaseModel):
    """The srstudio page, with the form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    number: int
    outcome: Outcome = Outcome()
    sent: SlugFields | None = None


def srstudio_url(slug: str, number: int) -> str:
    """The srstudio page of an episode."""
    return episodes_url(slug, number) + SRSTUDIO_SUFFIX


def fix_link(access: ChannelAccess, number: int, missing: Missing) -> str:
    """The page where a missing piece is fixed."""
    if missing.message in LIBRARY_FIXES:
        return channel_url(access.channel.slug, LIBRARY_SUFFIX)
    suffix = MEDIA_SUFFIX if missing.message == NO_STYLE else SHORT_SCRIPT_SUFFIX
    return episodes_url(access.channel.slug, number) + suffix


def checklist(
    access: ChannelAccess, number: int, missing: list[Missing], translate: Translator
) -> list[tuple[bool, str, str]]:
    """(done, label, href) for each missing piece, in the checklist component's form."""
    return [
        (False, translate(item.message, **item.values), fix_link(access, number, item))
        for item in missing
    ]


def srstudio_page(
    request: Request, app: Services, access: ChannelAccess, shown: SrstudioView
) -> Response:
    """The episode's slug and seed form, the missing pieces and, when none are, the download."""
    require(access, Permission.WRITE_CHANNEL)
    current = found_episode(app, access, shown.number)
    translate = translator(request)
    stored = get_episode_slug(app, access, shown.number)
    missing = missing_pieces(app, access, shown.number)
    context: dict[str, Any] = {
        "episode": current.episode,
        "stored": stored,
        "slug_value": shown.sent.slug if shown.sent and not stored.exported_at else stored.slug,
        "seed_value": shown.sent.seed if shown.sent else str(stored.seed),
        "max_seed": MAX_SEED,
        "errors": shown.outcome.errors,
        "action": srstudio_url(access.channel.slug, shown.number),
        "missing": checklist(access, shown.number, missing, translate),
        "zip_link": ZIP.format(slug=access.channel.slug, number=shown.number),
        **episode_frame(app, access, shown.number, translate),
        "episode_tab": "srstudio",
        **labels(),
    }
    view = View(
        template="workspace/episode_srstudio.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PAGE)
def srstudio_tab(request: Request, app: ServicesDep, access: PageAccess, number: int) -> Response:
    """What the episode needs before srstudio can render it, and the zip when it is ready."""
    return srstudio_page(request, app, access, SrstudioView(number=number))


def slug_errors(
    app: Services, access: ChannelAccess, number: int, fields: SlugFields, translate: Translator
) -> Outcome | None:
    """Save the slug and seed; the errors to show, or None when it worked."""
    try:
        data = EpisodeSlugInput.model_validate({"slug": fields.slug, "seed": fields.seed})
        save_episode_slug(app, access, number, data, revision_of(fields.revision))
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except SlugTaken:
        return Outcome.conflict({"slug": translate(SLUG_TAKEN)})
    except SlugLocked:
        return Outcome.conflict({"slug": translate(SLUG_LOCKED)})
    except PartChanged:
        return Outcome.conflict({"form": translate(SLUG_CHANGED)})
    return None


@router.post(PAGE)
def save_slug(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[SlugFields, Form()],
) -> Response:
    """Save the slug srstudio names the episode by and the seed that repeats its frames."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CHANNEL)
    found_episode(app, access, number)
    outcome = slug_errors(app, access, number, fields, translator(request))
    if outcome is None:
        return done(request, app, srstudio_url(access.channel.slug, number), Flash.SAVED)
    return srstudio_page(
        request, app, access, SrstudioView(number=number, outcome=outcome, sent=fields)
    )
