"""The readiness list on an episode's page: each gap in the person's language, linked to the page
that fixes it (rule project.readiness)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from sreditor.deps import ChannelAccess, Services
from sreditor.episode_facts import EpisodeFacts
from sreditor.i18n import N_, Translator
from sreditor.readiness import Area, ReadinessItem, Severity, readiness_report
from sreditor.web.workspace.shell import channel_url

LIBRARY_PAGE = "/library"
SEVERITY_LABELS: dict[Severity, str] = {
    Severity.BLOCKER: N_("Blocks export and render"),
    Severity.WARNING: N_("Warning"),
}
SEVERITY_TONES: dict[Severity, str] = {Severity.BLOCKER: "warning", Severity.WARNING: ""}
# Where each area is fixed: a page of the episode, or the channel's library.
EPISODE_PAGES: dict[Area, str] = {
    Area.CLAIMS: "/claims",
    Area.CREDITS: "/external-media",
    Area.DELIVERY: "/delivery",
    Area.SHORT_SCRIPT: "/short-script",
    Area.SRSTUDIO: "/srstudio",
}
AREA_LABELS: dict[Area, str] = {
    Area.CLAIMS: N_("Claims"),
    Area.CREDITS: N_("Third-party"),
    Area.DELIVERY: N_("Delivery"),
    Area.SHORT_SCRIPT: N_("Short script"),
    Area.LIBRARY: N_("Library"),
    Area.SRSTUDIO: "srstudio",  # a product name, the same in every language
}


class ReadinessRow(BaseModel):
    """One gap as the page shows it."""

    model_config = ConfigDict(frozen=True)

    message: str
    severity: str
    tone: str
    area: str
    href: str


def page_of(access: ChannelAccess, number: int, area: Area) -> str:
    """The page that fixes the area's gaps."""
    slug = access.channel.slug
    if area is Area.LIBRARY:
        return channel_url(slug, LIBRARY_PAGE)
    return channel_url(slug, f"/episodes/{number}{EPISODE_PAGES[area]}")


def row_of(
    access: ChannelAccess, number: int, found: ReadinessItem, translate: Translator
) -> ReadinessRow:
    """A gap with its message translated and the link to where it is fixed."""
    return ReadinessRow(
        message=translate(found.template, **found.values),
        severity=translate(SEVERITY_LABELS[found.severity]),
        tone=SEVERITY_TONES[found.severity],
        area=translate(AREA_LABELS[found.area]),
        href=page_of(access, number, found.area),
    )


def readiness_rows(
    app: Services, access: ChannelAccess, number: int, translate: Translator
) -> list[ReadinessRow]:
    """The episode's gaps, blockers first, for the page."""
    report = readiness_report(EpisodeFacts(app, access, number))
    return [row_of(access, number, found, translate) for found in report.items]
