"""The workspace's forms as Pydantic models (web.layout).

Every field is a string, so whatever a browser posts reaches the page, which converts it and
shows any error next to its field (web.forms.errors). Field names match the domain models the
values become, so a ValidationError from either one lands next to the right field.
"""

from __future__ import annotations

import re
import unicodedata

from fastapi import UploadFile
from pydantic import Field

from sreditor.channels import SLUG_LENGTH, Platform
from sreditor.tokens import DEFAULT_TTL_DAYS
from sreditor.web.forms import SubmittedFields

# Choices offered for a token's lifetime, in days; the store allows up to MAX_TTL_DAYS.
TOKEN_DAYS = (30, DEFAULT_TTL_DAYS, 180, 365)
NOT_SLUG = re.compile(r"[^a-z0-9]+")


def slug_from(name: str) -> str:
    """A channel address from its name: lowercase ASCII words joined by hyphens."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return NOT_SLUG.sub("-", ascii_name.lower()).strip("-")[:SLUG_LENGTH].strip("-")


def revision_of(value: str) -> int | None:
    """The revision a page was showing, or None when the form had none."""
    return int(value) if value.isdigit() else None


def platform_or_none(value: str) -> Platform | None:
    """The platform a form names, or None for the channel's own level."""
    try:
        return Platform(value)
    except ValueError:
        return None


class NewChannelFields(SubmittedFields):
    """Create a channel."""

    name: str = ""
    slug: str = ""
    language: str = ""


class ChannelFields(SubmittedFields):
    """A channel's name, language and status."""

    name: str = ""
    language: str = ""
    status: str = ""
    revision: str = ""


class DeleteFields(SubmittedFields):
    """Delete a channel: its address typed again, to be sure."""

    confirm_slug: str = ""
    revision: str = ""


class AccountFields(SubmittedFields):
    """A channel's account on one platform."""

    platform: str = ""
    handle: str = ""
    url: str = ""
    description: str = ""
    status: str = ""
    revision: str = ""


class RevisionFields(SubmittedFields):
    """A change that only needs the revision the page was showing."""

    revision: str = ""
    platform: str = ""


class UploadFields(SubmittedFields):
    """A brand file to store in a slot."""

    revision: str = ""
    platform: str = ""
    file: UploadFile | None = None


class ColorFields(SubmittedFields):
    """A brand color, at the channel's level or a platform's."""

    name: str = ""
    hex: str = ""
    platform: str = ""
    revision: str = ""


class GuidelineFields(SubmittedFields):
    """An editorial guideline, at the channel's level or a platform's."""

    kind: str = ""
    text: str = ""
    platform: str = ""


class RetireFields(SubmittedFields):
    """Retire one guideline."""

    statement_id: str = ""
    platform: str = ""


class EpisodeFields(SubmittedFields):
    """An episode's title, summary and status; a new episode leaves the status empty."""

    title: str = ""
    summary: str = ""
    status: str = ""
    revision: str = ""


class TextFields(SubmittedFields):
    """One of an episode's text parts, such as its research notes or screenplay."""

    text: str = ""
    revision: str = ""


class SourceFields(SubmittedFields):
    """A source to cite; an empty date means today."""

    url: str = ""
    title: str = ""
    claim: str = ""
    accessed_on: str = ""


class EntryFields(SubmittedFields):
    """A character or location; `episode` names the screenplay the person added it from."""

    name: str = ""
    description: str = ""
    revision: str = ""
    episode: str = ""


class RemoveSourceFields(SubmittedFields):
    """Stop citing one source."""

    source_id: str = ""
    revision: str = ""


class InviteFields(SubmittedFields):
    """Invite someone by email."""

    email: str = ""
    role: str = ""


class RoleFields(SubmittedFields):
    """A member's new role."""

    role: str = ""


class ProfileFields(SubmittedFields):
    """The person's display name."""

    display_name: str = ""


class PasswordFields(SubmittedFields):
    """Change the password."""

    current_password: str = ""
    password: str = ""


class TokenFields(SubmittedFields):
    """A new API token for an agent."""

    name: str = ""
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: str = ""


class WaiverFields(SubmittedFields):
    """A signed waiver to upload, or a waiver's new details; `revision` only when editing."""

    signer: str = ""
    covers: str = ""
    signed_on: str = ""
    revision: str = ""
    file: UploadFile | None = None


class StyleFields(SubmittedFields):
    """The visual style an episode picks; empty for none."""

    style_id: str = ""
    revision: str = ""


class MediaFields(SubmittedFields):
    """A media file to upload, or a file's new name and note; `revision` only when editing."""

    name: str = ""
    note: str = ""
    revision: str = ""
    file: UploadFile | None = None
