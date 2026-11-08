from __future__ import annotations

import io
import logging
import logging.config

import pytest
import uvicorn.config

from sreditor.mail import LogMailer
from sreditor.redact import REDACTED, RedactingFilter, logging_config, redact

SECRET = "Xy3_-abcDEF123"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"GET /verify?token={SECRET} HTTP/1.1", f"GET /verify?token={REDACTED} HTTP/1.1"),
        (
            f"/reset-password?lang=pt&token={SECRET}&x=1",
            f"/reset-password?lang=pt&token={REDACTED}&x=1",
        ),
        (f"https://a.test/verify#token={SECRET}", f"https://a.test/verify#token={REDACTED}"),
        (f"GET /invitations/{SECRET} HTTP/1.1", f"GET /invitations/{REDACTED} HTTP/1.1"),
        (f"/invitations/{SECRET}?next=/app", f"/invitations/{REDACTED}?next=/app"),
        (f"Your verification token: {SECRET}\n", f"Your verification token: {REDACTED}\n"),
        ("POST /invitations/accept HTTP/1.1", "POST /invitations/accept HTTP/1.1"),
        ("/invitations/accept?x=1", "/invitations/accept?x=1"),
        ("POST /invitations/open HTTP/1.1", "POST /invitations/open HTTP/1.1"),
        ("POST /invitations/join HTTP/1.1", "POST /invitations/join HTTP/1.1"),
        (
            f"https://a.test/invitations#token={SECRET}",
            f"https://a.test/invitations#token={REDACTED}",
        ),
        ("GET /channels/funny-memes HTTP/1.1", "GET /channels/funny-memes HTTP/1.1"),
        (f"PUT /files/{SECRET} HTTP/1.1", f"PUT /files/{REDACTED} HTTP/1.1"),
        (f"https://a.test/files/{SECRET}", f"https://a.test/files/{REDACTED}"),
        (
            "GET /app/channels/c/files/logo.png HTTP/1.1",
            "GET /app/channels/c/files/logo.png HTTP/1.1",
        ),
        ("csrf_token=abc and mytoken=abc", "csrf_token=abc and mytoken=abc"),
    ],
)
def test_redact_text_returns_expected_text(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_redact_invitation_token_starting_with_accept_is_redacted() -> None:
    assert redact("/invitations/acceptXYZ") == f"/invitations/{REDACTED}"


def record(message: object, args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, message, args, None)  # type: ignore[arg-type]


def test_filter_positional_arguments_are_redacted_and_record_is_kept() -> None:
    entry = record("%s %s %d", (f"/verify?token={SECRET}", "ok", 200))
    assert RedactingFilter().filter(entry) is True
    assert entry.getMessage() == f"/verify?token={REDACTED} ok 200"


def test_filter_named_arguments_are_redacted() -> None:
    named = record("%(path)s", ({"path": f"/invitations/{SECRET}"},))
    RedactingFilter().filter(named)
    assert named.getMessage() == f"/invitations/{REDACTED}"


def test_filter_non_string_message_leaves_arguments_empty() -> None:
    opaque = record(ValueError(f"token={SECRET}"), None)
    RedactingFilter().filter(opaque)
    assert opaque.args is None


def logged_through(reveal: list[str]) -> tuple[str, str]:
    """Log one access line and one mail with a token; return the access and default output."""
    access, default = io.StringIO(), io.StringIO()
    config = logging_config(uvicorn.config.LOGGING_CONFIG, reveal=reveal)
    config["handlers"]["access"]["stream"] = access
    config["handlers"]["default"]["stream"] = default
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        logging.config.dictConfig(config)
        args = ("127.0.0.1:5000", "GET", f"/verify?token={SECRET}", "1.1", 200)
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', *args)
        LogMailer(is_revealing_tokens=True).send(
            "a@b.test", "Verify", f"https://x.test/verify?token={SECRET}"
        )
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    return access.getvalue(), default.getvalue()


def test_logging_config_default_redacts_access_log() -> None:
    access, _ = logged_through(reveal=[])
    assert SECRET not in access and f"token={REDACTED}" in access


def test_logging_config_default_redacts_application_log() -> None:
    _, default = logged_through(reveal=[])
    assert SECRET not in default and "Email to a@b.test" in default


def test_logging_config_leaves_uvicorn_defaults_unchanged() -> None:
    logging_config(uvicorn.config.LOGGING_CONFIG)
    assert "filters" not in uvicorn.config.LOGGING_CONFIG["handlers"]["access"]


def test_log_mailer_send_redacts_token_unless_told_to_reveal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = f"Open https://x.test/reset-password?token={SECRET}"
    with caplog.at_level(logging.WARNING, logger="sreditor.mail"):
        LogMailer().send("a@b.test", "Reset", body)
        LogMailer(is_revealing_tokens=True).send("a@b.test", "Reset", body)
    first, second = (entry.getMessage() for entry in caplog.records)
    assert SECRET not in first and REDACTED in first
    assert SECRET in second


def test_logging_config_reveal_keeps_mail_links() -> None:
    _, default = logged_through(reveal=["sreditor.mail"])
    assert f"verify?token={SECRET}" in default


def test_logging_config_reveal_still_redacts_access_log() -> None:
    access, _ = logged_through(reveal=["sreditor.mail"])
    assert SECRET not in access
