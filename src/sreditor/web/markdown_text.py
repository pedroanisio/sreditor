"""Markdown in the long texts people and agents write, shown the same on pages and in PDF books
(rule project.text.markdown).

The text comes from people and from agents, so the renderer turns raw HTML into plain text,
draws no images (a page would fetch them from anywhere; the PDF fetches nothing outside the
app) and keeps only links a reader can follow safely.
"""

from __future__ import annotations

from jinja2 import Environment
from markdown_it import MarkdownIt
from markupsafe import Markup

# Links a reader may follow; anything else, such as javascript: or a relative path that would
# resolve against sreditor itself, stays as plain text.
SAFE_LINK_PREFIXES = ("http://", "https://", "mailto:")
MARKDOWN_FILTER = "markdown"
MARKDOWN_PLAIN_FILTER = "markdown_plain"


class SafeMarkdown(MarkdownIt):
    """CommonMark without raw HTML or images, keeping only http, https and mailto links."""

    def __init__(self) -> None:
        super().__init__("commonmark", {"html": False})
        self.disable("image")

    def validateLink(self, url: str) -> bool:  # markdown-it names it so; overriding keeps it
        """Whether a link may stay a link."""
        return url.strip().lower().startswith(SAFE_LINK_PREFIXES)


RENDERER = SafeMarkdown()


def markdown_html(text: str) -> Markup:
    """The text rendered from Markdown as HTML that is safe to put in a page."""
    # Safe because the renderer escapes every raw character and allows no HTML of its own.
    return Markup(RENDERER.render(text))


def markdown_plain(text: str) -> str:
    """The text with its Markdown markup removed, for places that cut it short."""
    return markdown_html(text).striptags()


def add_markdown_filters(jinja: Environment) -> None:
    """Let templates show Markdown text as `text | markdown` or `text | markdown_plain`."""
    jinja.filters[MARKDOWN_FILTER] = markdown_html
    jinja.filters[MARKDOWN_PLAIN_FILTER] = markdown_plain
