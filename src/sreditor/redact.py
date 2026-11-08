"""Remove one-time tokens from log output.

Email links carry tokens (`?token=`, `#token=`, `/invitations/<token>`); a logged token can be
replayed to verify an email, reset a password or join a channel. Signed file links carry theirs
in the path (`/files/<secret>`), and a logged one can fetch or upload a file while it lasts.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[redacted]"

PATTERNS = (
    # token=<value> in a query string or fragment, and "token: <value>" in plain text.
    re.compile(r"(?P<keep>\btoken(?:=|: ))[^\s&#\"'<>]+"),
    # /invitations/<token> from older links, but not the named invitation endpoints.
    re.compile(r"(?P<keep>/invitations/)(?!(?:accept|open|join)(?:[/?#\s\"']|$))[^\s/?#&\"'<>]+"),
    # /files/<secret>, the signed file links of agentic.mcp.files, at the root of a path or URL
    # only: /app/channels/<slug>/files/<name> and /brand/files/<slot> name files, not secrets.
    re.compile(r"(?P<keep>(?:^|(?<=[\s\"'(=])|https?://[^/\s\"'<>]+)/files/)[^\s/?#&\"'<>]+"),
)


def redact(text: str) -> str:
    """Replace each secret token in the text with a placeholder, keeping the text around it."""
    for pattern in PATTERNS:
        text = pattern.sub(lambda match: match["keep"] + REDACTED, text)
    return text


def _redact_value(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


class RedactingFilter(logging.Filter):
    """Redact tokens in a record's message and arguments before any handler formats it.

    Records from the loggers in `reveal` pass untouched: a development setting such as
    SREDITOR_MAIL_LOG_TOKENS opts the mail logger out, while access logs stay redacted.
    """

    def __init__(self, reveal: Iterable[str] = ()) -> None:
        super().__init__()
        self.reveal = frozenset(reveal)

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record before it is formatted, unless its logger is in `reveal`."""
        if record.name in self.reveal:
            return True
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_value(arg) for key, arg in record.args.items()}
        return True


def logging_config(base: dict[str, Any], reveal: Iterable[str] = ()) -> dict[str, Any]:
    """A copy of a dictConfig whose handlers all redact, and whose root logger uses them.

    Routing the root logger through the default handler also covers application loggers
    such as `sreditor.mail`, which otherwise print through Python's unfiltered fallback.
    Loggers named in `reveal` are left unredacted.
    """
    config = copy.deepcopy(base)
    config.setdefault("filters", {})["redact"] = {
        "()": f"{__name__}.RedactingFilter",
        "reveal": sorted(reveal),
    }
    for handler in config.get("handlers", {}).values():
        handler.setdefault("filters", []).append("redact")
    config.setdefault("root", {"handlers": ["default"], "level": "INFO"})
    return config
