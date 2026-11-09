"""An episode's reviews page: who reviewed it, what they saw and decided (rule project.reviews).

Reviews are append-only, so the page adds and deletes them but never edits. Reviews only warn
when an episode moves to published; nothing here blocks a status.
"""

from __future__ import annotations

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.delivery_operations import episode_images, episode_videos
from sreditor.deps import ChannelAccess, RoleMissing, Services, ServicesDep
from sreditor.i18n import N_, Translator
from sreditor.members import Permission
from sreditor.pagination import DEFAULT_LIMIT
from sreditor.review_operations import (
    ReviewQuery,
    ReviewRef,
    ReviewRefused,
    add_review,
    list_reviews,
    remove_review,
    review_summary,
)
from sreditor.reviews import NOTES_LENGTH, Review, ReviewInput, ReviewNotFound, SeenRange, Verdict
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.labels import VERDICT_LABELS, VERDICT_TONES
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.episodes import episode_frame, episodes_url, found_episode, labels
from sreditor.web.workspace.shell import (
    Flash,
    Outcome,
    PageAccess,
    PageForbidden,
    View,
    channel_page,
    done,
    require,
)

router = APIRouter()

REVIEWS_SUFFIX = "/reviews"
PAGE = "/app/channels/{slug}/episodes/{number}/reviews"
HASH_PREFIX_LENGTH = 8
# One range per line: "0 - 3.5", "0-3.5", "0 to 3.5" or "0 3.5", in seconds.
RANGE_LINE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:-|–|to|\s)\s*(\d+(?:\.\d+)?)\s*$")
REVIEW_GONE = N_("This review was already deleted. Reload the page.")
BAD_RANGE = N_("Write one range per line, in seconds, such as 0 - 3.5.")
CHOOSE_FILE = N_("Choose a file of this episode.")
PAST_THE_END = N_("A range ends past the end of the file.")
IN_THE_FUTURE = N_("A review cannot be dated in the future.")
AN_API_TOKEN = N_("an API token")
FIELD_MESSAGES = {
    "media_id": CHOOSE_FILE,
    "end_s": PAST_THE_END,
    "reviewed_at": IN_THE_FUTURE,
}


class ReviewFields(SubmittedFields):
    """A review as posted: the verdict, notes, the file and the ranges seen, one per line."""

    verdict: str = Verdict.APPROVED.value
    notes: str = ""
    media_id: str = ""
    seen: str = ""


class ReviewRemovalFields(SubmittedFields):
    """Delete one review."""

    review_id: str = ""


class ReviewsView(BaseModel):
    """The reviews page, with the form that was sent and its errors."""

    model_config = ConfigDict(frozen=True)

    number: int
    cursor: str = ""
    outcome: Outcome = Outcome()
    sent: ReviewFields | None = None


def reviews_url(slug: str, number: int) -> str:
    """The reviews page of an episode."""
    return episodes_url(slug, number) + REVIEWS_SUFFIX


def parse_ranges(text: str) -> list[SeenRange]:
    """The ranges in a textarea, one per line; ValueError when a line is not one."""
    found = []
    for line in text.splitlines():
        if not line.strip():
            continue
        matched = RANGE_LINE.match(line)
        if matched is None:
            raise ValueError(line)
        found.append(SeenRange(start_s=float(matched[1]), end_s=float(matched[2])))
    return found


def review_input(fields: ReviewFields) -> ReviewInput:
    """The review a form describes; ValidationError names the field at fault."""
    return ReviewInput.model_validate(
        {
            "verdict": fields.verdict,
            "notes": fields.notes,
            "media_id": fields.media_id or None,
            "seen": [range_.model_dump() for range_ in parse_ranges(fields.seen)],
        }
    )


def file_options(
    app: Services, access: ChannelAccess, number: int, translate: Translator
) -> list[tuple[str, str]]:
    """(id, label) of the episode's videos and images a review may be about, after "no file"."""
    slug = access.channel.slug
    files = [*episode_videos(app, slug, number), *episode_images(app, slug, number)]
    return [("", translate(N_("No file")))] + [
        (str(file.id), f"{file.name} · {file.sha256[:HASH_PREFIX_LENGTH]}") for file in files
    ]


def reviewer_names(app: Services, reviewer_ids: set[UUID]) -> dict[str, str]:
    """The display name of each reviewer, by id; a user who is gone has none."""
    people = {reviewer_id: app.users.get(reviewer_id) for reviewer_id in reviewer_ids}
    return {
        str(reviewer_id): person.display_name for reviewer_id, person in people.items() if person
    }


def range_labels(review: Review) -> list[str]:
    """The ranges a review saw as short labels, such as "0–5.5 s"."""
    return [f"{seen.start_s:g}–{seen.end_s:g} s" for seen in review.seen]


def reviews_context(
    app: Services, access: ChannelAccess, shown: ReviewsView, translate: Translator
) -> dict[str, Any]:
    """The reviews, who wrote them, what a person may delete and the form's options."""
    query = ReviewQuery(number=shown.number, cursor=shown.cursor or None, limit=DEFAULT_LIMIT)
    found = list_reviews(app, access, query)
    is_owner = access.can(Permission.MANAGE_OWNERS)
    can_write = access.can(Permission.WRITE_EDITORIAL)
    return {
        "reviews": found.items,
        "next_cursor": found.next_cursor,
        "reviewers": reviewer_names(app, {review.reviewer_id for review in found.items}),
        "deletable": {
            str(review.id)
            for review in found.items
            if is_owner or (can_write and review.reviewer_id == access.user.id)
        },
        "seen_labels": {str(review.id): range_labels(review) for review in found.items},
        "latest": review_summary(app, access, shown.number).latest,
        "verdict_options": [(item.value, translate(VERDICT_LABELS[item])) for item in Verdict],
        "verdict_labels": {item.value: VERDICT_LABELS[item] for item in Verdict},
        "verdict_tones": {item.value: VERDICT_TONES[item] for item in Verdict},
        "file_options": file_options(app, access, shown.number, translate),
        "reviews_link": reviews_url(access.channel.slug, shown.number),
        "sent_review": shown.sent,
        "errors": shown.outcome.errors,
        "notes_limit": NOTES_LENGTH,
        "an_api_token": translate(AN_API_TOKEN),
    }


def reviews_page(
    request: Request, app: Services, access: ChannelAccess, shown: ReviewsView
) -> Response:
    """The episode's reviews with the form to add one and, where allowed, to delete them."""
    current = found_episode(app, access, shown.number)
    translate = translator(request)
    context = {
        "episode": current.episode,
        **episode_frame(app, access, shown.number, translate),
        "episode_tab": "reviews",
        **reviews_context(app, access, shown, translate),
        **labels(),
    }
    view = View(
        template="workspace/reviews.html",
        section="episodes",
        status_code=shown.outcome.status_code,
        context=context,
    )
    return channel_page(request, app, access, view)


@router.get(PAGE)
def reviews(
    request: Request, app: ServicesDep, access: PageAccess, number: int, cursor: str = ""
) -> Response:
    """An episode's reviews."""
    return reviews_page(request, app, access, ReviewsView(number=number, cursor=cursor))


def refusal_errors(refusal: ReviewRefused, translate: Translator) -> dict[str, str]:
    """A refused review as errors by field; one with no field of its own is the form's."""
    field = str(refusal.location[-1])
    if field in FIELD_MESSAGES:
        return {("seen" if field == "end_s" else field): translate(FIELD_MESSAGES[field])}
    return {"form": str(refusal)}


def range_errors(refusal: ValueError, translate: Translator) -> dict[str, str]:
    """Why a review was refused: a ranges line that is none, or a file or time that cannot be."""
    if isinstance(refusal, ReviewRefused):
        return refusal_errors(refusal, translate)
    return {"seen": translate(BAD_RANGE)}


@router.post(PAGE)
def new_review(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[ReviewFields, Form()],
) -> Response:
    """Add a review."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.WRITE_EDITORIAL)
    found_episode(app, access, number)
    translate = translator(request)
    try:
        add_review(app, access, number, review_input(fields))
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translate))
    except ValueError as refusal:
        outcome = Outcome.invalid(range_errors(refusal, translate))
    else:
        return done(request, app, reviews_url(access.channel.slug, number), Flash.REVIEW_ADDED)
    shown = ReviewsView(number=number, outcome=outcome, sent=fields)
    return reviews_page(request, app, access, shown)


@router.post(f"{PAGE}/delete")
def drop_review(
    request: Request,
    app: ServicesDep,
    access: PageAccess,
    number: int,
    fields: Annotated[ReviewRemovalFields, Form()],
) -> Response:
    """Delete a review."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    require(access, Permission.READ)
    found_episode(app, access, number)
    try:
        remove_review(app, access, ReviewRef(number=number, review_id=UUID(fields.review_id)))
    except RoleMissing as missing:
        raise PageForbidden(missing.permission) from missing
    except (ValueError, ReviewNotFound):
        outcome = Outcome.conflict({"form": translator(request)(REVIEW_GONE)})
        return reviews_page(request, app, access, ReviewsView(number=number, outcome=outcome))
    return done(request, app, reviews_url(access.channel.slug, number), Flash.REMOVED)
