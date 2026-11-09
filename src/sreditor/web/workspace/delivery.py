"""An episode's delivery page: its cuts and where each was posted (rule project.delivery).

The package is stored and revised as one part, so every dialog sends the package's revision and a
change made meanwhile is refused instead of overwritten. Gaps only warn, never block a status.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.channels import Platform
from sreditor.delivery import (
    ALT_TEXT_LENGTH,
    CAPTION_LENGTH,
    CUT_NAME_LENGTH,
    MAX_POST_HASHTAGS,
    WARNING_MESSAGES,
    Cut,
    Delivery,
    DeliveryInput,
    Post,
)
from sreditor.delivery_operations import (
    AI_NOTICE_REQUIRED,
    DeliveryRefused,
    episode_images,
    episode_videos,
    get_delivery,
    with_cut,
    with_post,
    without_cut,
    without_post,
    write_delivery,
)
from sreditor.deps import ChannelAccess, Services, ServicesDep
from sreditor.episode_texts import PartChanged
from sreditor.i18n import N_, Translator
from sreditor.media import MediaFile
from sreditor.members import Permission
from sreditor.problems import ProblemError
from sreditor.web.forms import field_errors
from sreditor.web.labels import platform_name
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.forms import CutFields, PostFields, RevisionFields, revision_of
from sreditor.web.workspace.media import media_url
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageNotFound,
    View,
    channel_page,
    done,
    refused,
    require,
)

router = APIRouter()

DELIVERY_SUFFIX = "/delivery"
PAGE = "/app/channels/{slug}/episodes/{number}/delivery"
HASHTAG_SEPARATORS = re.compile(r"[\s,]+")
DATETIME_LOCAL = "%Y-%m-%dT%H:%M"
DELIVERY_CHANGED = N_(
    "Someone changed the delivery package after you opened the page. Reload the page."
)
CUT_EXISTS = N_("A cut with this name already exists.")
CHOOSE_VIDEO = N_("Choose a video file of this episode.")
CHOOSE_IMAGE = N_("Choose an image file of this episode.")
FIELD_MESSAGES = {"video_id": CHOOSE_VIDEO, "cover_id": CHOOSE_IMAGE}
NO_FILE = ""


def delivery_url(slug: str, number: int) -> str:
    """The delivery page of an episode."""
    return episodes_url(slug, number) + DELIVERY_SUFFIX


def platform_of(value: str) -> Platform:
    """The platform in a path; a value that is none is a page that does not exist."""
    try:
        return Platform(value)
    except ValueError as unknown:
        raise PageNotFound(value) from unknown


class CutPath(BaseModel):
    """The episode and cut a form's address names."""

    model_config = ConfigDict(frozen=True)

    number: int
    name: str


def cut_path(number: int, name: str) -> CutPath:
    """The episode and cut in the address."""
    return CutPath(number=number, name=name)


class PostFormPath(BaseModel):
    """The episode and platform a form's address names."""

    model_config = ConfigDict(frozen=True)

    number: int
    platform: Platform


def post_path(number: int, platform: str) -> PostFormPath:
    """The episode and platform in the address."""
    return PostFormPath(number=number, platform=platform_of(platform))


# Path values in one parameter each, so handlers keep to five (code.functions.args).
CutTarget = Annotated[CutPath, Depends(cut_path)]
PostTarget = Annotated[PostFormPath, Depends(post_path)]


class DeliveryView(BaseModel):
    """The delivery page, with the dialog that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    number: int
    outcome: Outcome = Outcome()
    sent_cut: CutFields | None = None
    editing: str = ""  # the name of the cut whose dialog was sent; empty for the new-cut dialog
    sent_post: PostFields | None = None
    posting: str = ""  # the platform whose dialog was sent


def hashtag_text(hashtags: list[str]) -> str:
    """Hashtags as people type them, "#funny #dogs"."""
    return " ".join(f"#{tag}" for tag in hashtags)


def cut_values(cut: Cut, revision: int) -> CutFields:
    """A stored cut as the form that edits it."""
    return CutFields(
        name=cut.name,
        video_id=str(cut.video_id or NO_FILE),
        cover_id=str(cut.cover_id or NO_FILE),
        caption=cut.caption,
        alt_text=cut.alt_text,
        hashtags=hashtag_text(cut.hashtags),
        revision=str(revision),
    )


def post_values(post: Post | None, revision: int) -> PostFields:
    """A stored post, or none yet, as the form that edits it."""
    if post is None:
        return PostFields(revision=str(revision))
    moment = post.published_at.strftime(DATETIME_LOCAL) if post.published_at else ""
    return PostFields(
        cut=post.cut,
        url=str(post.url or ""),
        published_at=moment,
        caption=post.caption,
        alt_text=post.alt_text,
        hashtags=hashtag_text(post.hashtags),
        revision=str(revision),
    )


def file_options(files: list[MediaFile], none_label: str) -> list[tuple[str, str]]:
    """(id, name) choices for a cut's video or cover, with "no file" first."""
    return [(NO_FILE, none_label), *((str(file.id), file.name) for file in files)]


def account_rows(access: ChannelAccess, package: Delivery, revision: int) -> list[dict[str, Any]]:
    """One row per platform account of the channel, with its post and the form that edits it."""
    posts = {post.platform: post for post in package.posts}
    return [
        {
            "platform": account.platform.value,
            "name": platform_name(account.platform),
            "post": posts.get(account.platform),
            "values": post_values(posts.get(account.platform), revision),
        }
        for account in access.channel.accounts
    ]


def warning_texts(package: Delivery, translate: Translator) -> list[str]:
    """The package's warnings in the person's language."""
    return [translate(WARNING_MESSAGES[found.kind], **found.values) for found in package.warnings]


def delivery_context(
    app: Services, access: ChannelAccess, shown: DeliveryView, translate: Translator
) -> dict[str, Any]:
    """What the delivery template needs: the package, the files to pick from and the forms."""
    slug, number = access.channel.slug, shown.number
    package = get_delivery(app, access, number)
    videos, images = episode_videos(app, slug, number), episode_images(app, slug, number)
    sent = shown.sent_cut
    return {
        "package": package,
        "warnings": warning_texts(package, translate),
        "files": {str(file.id): file for file in [*videos, *images]},
        "video_options": file_options(videos, translate(N_("No video yet"))),
        "cover_options": file_options(images, translate(N_("No cover"))),
        "cut_values": {
            cut.name: sent
            if sent and shown.editing == cut.name
            else cut_values(cut, package.revision)
            for cut in package.cuts
        },
        "cut_options": [(cut.name, cut.name) for cut in package.cuts],
        "accounts": account_rows(access, package, package.revision),
        "sent_cut": sent,
        "sent_post": shown.sent_post,
        "posting": shown.posting,
        "editing": shown.editing,
        "is_new_cut": sent is not None and not shown.editing,
        "delivery_link": delivery_url(slug, number),
        "media_link": media_url(slug, number),
        "caption_limit": CAPTION_LENGTH,
        "alt_text_limit": ALT_TEXT_LENGTH,
        "name_limit": CUT_NAME_LENGTH,
        "post_hashtag_limit": MAX_POST_HASHTAGS,
    }


def delivery_page(
    request: Request, app: Services, access: ChannelAccess, shown: DeliveryView
) -> Response:
    """The episode's cuts and posts, with dialogs to change them for people who may."""
    current = found_episode(app, access, shown.number)
    translate = translator(request)
    context = {
        "episode": current.episode,
        "errors": shown.outcome.errors,
        **delivery_context(app, access, shown, translate),
        **episode_frame(app, access, shown.number, translate),
        "episode_tab": "delivery",
        **labels(),
    }
    view = View(
        template="workspace/delivery.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PAGE)
def delivery(request: Request, app: ServicesDep, access: PageAccess, number: int) -> Response:
    """An episode's cuts and where each was posted."""
    return delivery_page(request, app, access, DeliveryView(number=number))


def hashtag_list(text: str) -> list[str]:
    """The hashtags in a form's one text, split on spaces and commas."""
    return [tag for tag in HASHTAG_SEPARATORS.split(text.strip()) if tag]


def as_cut(fields: CutFields, name: str) -> Cut:
    """What a form says about a cut; an empty file is none."""
    return Cut.model_validate(
        {
            "name": name,
            "video_id": fields.video_id or None,
            "cover_id": fields.cover_id or None,
            "caption": fields.caption,
            "alt_text": fields.alt_text,
            "hashtags": hashtag_list(fields.hashtags),
        }
    )


def as_post(fields: PostFields, platform: Platform) -> Post:
    """What a form says about a post; an empty link or time is none."""
    return Post.model_validate(
        {
            "platform": platform,
            "cut": fields.cut,
            "url": fields.url.strip() or None,
            "published_at": fields.published_at or None,
            "caption": fields.caption,
            "alt_text": fields.alt_text,
            "hashtags": hashtag_list(fields.hashtags),
        }
    )


def refusal_errors(refusal: DeliveryRefused, translate: Translator) -> dict[str, str]:
    """A refused package as errors by field; one with no field of its own is the form's."""
    field = str(refusal.location[-1]) if refusal.location else "form"
    if field in FIELD_MESSAGES:
        return {field: translate(FIELD_MESSAGES[field])}
    if field == "caption":
        return {field: translate(AI_NOTICE_REQUIRED, **refusal.values)}
    # The package's own rules are static, N_-marked English, so the message is its msgid.
    return {"form": translate(str(refusal))}


def change_package(
    app: Services,
    access: ChannelAccess,
    number: int,
    change: Callable[[DeliveryInput], DeliveryInput],
    revision: int | None,
) -> None:
    """Apply a change to the package as it is now and save it at the revision the page showed."""
    package = get_delivery(app, access, number)
    current = DeliveryInput(cuts=package.cuts, posts=package.posts)
    write_delivery(app, access, number, change(current), revision)


def change_outcome(request: Request, action: Callable[[], None]) -> Outcome | None:
    """Run a change; the errors to show, or None when it worked."""
    translate = translator(request)
    try:
        action()
    except ValidationError as invalid:
        return Outcome.invalid(field_errors(invalid, translate))
    except DeliveryRefused as refusal:
        return Outcome.invalid(refusal_errors(refusal, translate))
    except PartChanged:
        return Outcome.conflict({"form": translate(DELIVERY_CHANGED)})
    except ProblemError as problem:
        return refused(problem, translate)
    return None


def changed(
    request: Request, access: ChannelAccess, number: int, flash: Flash, app: Services
) -> Response:
    """Redirect to the page, saying what happened."""
    return done(request, app, delivery_url(access.channel.slug, number), flash)


def add_cut_to(package: DeliveryInput, cut: Cut) -> DeliveryInput:
    """The package with a new cut; its name must not be taken, so no cut is replaced by mistake."""
    if any(other.name == cut.name for other in package.cuts):
        raise DeliveryRefused(("cuts", len(package.cuts), "name"), CUT_EXISTS)
    return with_cut(package, cut)


@router.post(f"{PAGE}/cuts")
def new_cut(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[CutFields, Form()],
) -> Response:
    """Add a cut to the package."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    found_episode(app, access, number)
    revision = revision_of(fields.revision)
    outcome = change_outcome(
        request,
        lambda: change_package(
            app, access, number, lambda now: add_cut_to(now, as_cut(fields, fields.name)), revision
        ),
    )
    if outcome is None:
        return changed(request, access, number, Flash.SAVED, app)
    return delivery_page(
        request, app, access, DeliveryView(number=number, outcome=outcome, sent_cut=fields)
    )


@router.post(f"{PAGE}/cuts/{{name}}")
def save_cut(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: CutTarget,
    fields: Annotated[CutFields, Form()],
) -> Response:
    """Change a cut's video, cover, caption, alt text or hashtags."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    number = target.number
    found_episode(app, access, number)
    revision = revision_of(fields.revision)
    outcome = change_outcome(
        request,
        lambda: change_package(
            app, access, number, lambda now: with_cut(now, as_cut(fields, target.name)), revision
        ),
    )
    if outcome is None:
        return changed(request, access, number, Flash.SAVED, app)
    shown = DeliveryView(number=number, outcome=outcome, sent_cut=fields, editing=target.name)
    return delivery_page(request, app, access, shown)


@router.post(f"{PAGE}/cuts/{{name}}/remove")
def drop_cut(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: CutTarget,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Take a cut out of the package; refused while a post still uses it."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    number = target.number
    found_episode(app, access, number)
    revision = revision_of(fields.revision)
    outcome = change_outcome(
        request,
        lambda: change_package(
            app, access, number, lambda now: without_cut(now, target.name), revision
        ),
    )
    if outcome is None:
        return changed(request, access, number, Flash.REMOVED, app)
    return delivery_page(request, app, access, DeliveryView(number=number, outcome=outcome))


@router.post(f"{PAGE}/posts/{{platform}}")
def save_post(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: PostTarget,
    fields: Annotated[PostFields, Form()],
) -> Response:
    """Set the post on one platform account: its cut, link and publish time."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    number = target.number
    found_episode(app, access, number)
    revision = revision_of(fields.revision)
    outcome = change_outcome(
        request,
        lambda: change_package(
            app,
            access,
            number,
            lambda now: with_post(now, as_post(fields, target.platform)),
            revision,
        ),
    )
    if outcome is None:
        return changed(request, access, number, Flash.SAVED, app)
    shown = DeliveryView(
        number=number, outcome=outcome, sent_post=fields, posting=target.platform.value
    )
    return delivery_page(request, app, access, shown)


@router.post(f"{PAGE}/posts/{{platform}}/remove")
def drop_post(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    target: PostTarget,
    fields: Annotated[RevisionFields, Form()],
) -> Response:
    """Take a platform's post out of the package."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_CONTENT)
    number = target.number
    found_episode(app, access, number)
    revision = revision_of(fields.revision)
    outcome = change_outcome(
        request,
        lambda: change_package(
            app, access, number, lambda now: without_post(now, target.platform), revision
        ),
    )
    if outcome is None:
        return changed(request, access, number, Flash.REMOVED, app)
    return delivery_page(request, app, access, DeliveryView(number=number, outcome=outcome))
