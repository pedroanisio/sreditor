"""Fix the srstudio slugs an export carried (rule project.srstudio.slugs).

srstudio finds episodes, bible entries and props by slug, so once a zip has named them a new
slug would orphan what srstudio already built. The zip names the episode, every published
episode the bible's facts cite, every character and place of the bible, and the props its
beats use.
"""

from __future__ import annotations

from sreditor.deps import Services
from sreditor.library import LibraryKind
from sreditor.srstudio import EpisodeSlug, ExportedSheet
from sreditor.srstudio_operations import all_entries, published_numbers

BIBLE_KINDS = (LibraryKind.CHARACTER, LibraryKind.LOCATION)


def exported_episodes(app: Services, channel: str, number: int) -> list[EpisodeSlug]:
    """The episode itself and the published episodes its bible cites as reels."""
    numbers = sorted({number} | published_numbers(app, channel))
    return [
        app.srstudio.episode_slug(channel, each, app.episodes.get(channel, each).episode.title)
        for each in numbers
    ]


def beat_props(app: Services, channel: str, number: int) -> set[str]:
    """The prop slugs the episode's beats use."""
    script = app.short_scripts.short_script(channel, number)
    return {beat.prop for block in script.blocks for beat in block.beats if beat.prop}


def exported_sheets(app: Services, channel: str, number: int) -> list[ExportedSheet]:
    """The bible's characters and places, and the props the beats use, with their sheets."""
    entries = [entry for kind in BIBLE_KINDS for entry in all_entries(app, channel, kind)]
    sheets = app.srstudio.sheets(entries)
    used = beat_props(app, channel, number)
    props = all_entries(app, channel, LibraryKind.PROP)
    prop_sheets = app.srstudio.sheets(props)
    return [ExportedSheet(entry=entry, sheet=sheets[entry.id]) for entry in entries] + [
        ExportedSheet(entry=entry, sheet=prop_sheets[entry.id])
        for entry in props
        if prop_sheets[entry.id].slug in used
    ]


def lock_exported_slugs(app: Services, channel: str, number: int) -> None:
    """Fix every slug the episode's srstudio zip carried."""
    app.srstudio.mark_exported(
        exported_episodes(app, channel, number), exported_sheets(app, channel, number)
    )
