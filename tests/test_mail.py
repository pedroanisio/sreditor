"""Outgoing email through SMTP2GO, on worker threads, and the choice of mailer."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Self

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sreditor.api import build_mailer, create_app
from sreditor.mail import (
    API_KEY_HEADER,
    BackgroundMailer,
    LogMailer,
    MailDeliveryError,
    MailSettings,
    Smtp2GoMailer,
    post,
)
from sreditor.settings import AppSettings, mail_settings

API_KEY = "api-test-key"
SENDER = "sreditor <noreply@sreditor.test>"
ACCEPTED = json.dumps({"data": {"succeeded": 1, "failed": 0, "failures": []}}).encode()
DELIVERING = MailSettings(smtp2go_api_key=API_KEY, sender=SENDER)


class RecordingTransport:
    """Stands in for the HTTP call and answers with a fixed body."""

    def __init__(self, answer: bytes = ACCEPTED) -> None:
        self.answer = answer
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        self.requests.append(request)
        return self.answer


def sent_payload(transport: RecordingTransport) -> dict[str, Any]:
    data = transport.requests[0].data
    assert isinstance(data, bytes)
    payload: dict[str, Any] = json.loads(data)
    return payload


def test_smtp2go_send_posts_plain_text_email() -> None:
    transport = RecordingTransport()
    Smtp2GoMailer(DELIVERING, transport).send("ana@example.com", "Verify", "Open the link")
    assert sent_payload(transport) == {
        "sender": SENDER,
        "to": ["ana@example.com"],
        "subject": "Verify",
        "text_body": "Open the link",
    }


def test_smtp2go_send_passes_api_key_in_header() -> None:
    transport = RecordingTransport()
    Smtp2GoMailer(DELIVERING, transport).send("ana@example.com", "Verify", "Body")
    request = transport.requests[0]
    assert request.get_header(API_KEY_HEADER.capitalize()) == API_KEY
    assert request.full_url == "https://api.smtp2go.com/v3/email/send"
    assert request.get_method() == "POST"


@pytest.mark.parametrize(
    "answer",
    [
        {"data": {"succeeded": 0, "failed": 1, "failures": ["bad address"]}},
        {"data": {}},
    ],
)
def test_smtp2go_send_answer_without_success_raises(answer: dict[str, Any]) -> None:
    mailer = Smtp2GoMailer(DELIVERING, RecordingTransport(json.dumps(answer).encode()))
    with pytest.raises(MailDeliveryError, match="did not send"):
        mailer.send("ana@example.com", "Verify", "Body")


def test_smtp2go_send_unreadable_answer_raises() -> None:
    mailer = Smtp2GoMailer(DELIVERING, RecordingTransport(b"<html>"))
    with pytest.raises(MailDeliveryError, match="unreadable"):
        mailer.send("ana@example.com", "Verify", "Body")


def test_smtp2go_mailer_without_key_is_refused() -> None:
    with pytest.raises(ValueError, match="API key"):
        Smtp2GoMailer(MailSettings())


def test_mail_settings_key_without_sender_is_rejected() -> None:
    with pytest.raises(ValidationError, match="SREDITOR_MAIL_SENDER"):
        MailSettings(smtp2go_api_key=API_KEY)


def test_mail_settings_plain_http_url_is_rejected() -> None:
    with pytest.raises(ValidationError, match="https"):
        MailSettings(smtp2go_api_key=API_KEY, sender=SENDER, smtp2go_url="http://api.test/")


def test_mail_settings_key_is_hidden_in_repr() -> None:
    assert API_KEY not in repr(DELIVERING)


def test_mail_settings_from_environment_reads_key_sender_and_url() -> None:
    settings = mail_settings(
        {
            "SREDITOR_SMTP2GO_API_KEY": API_KEY,
            "SREDITOR_MAIL_SENDER": SENDER,
            "SREDITOR_SMTP2GO_URL": "https://eu-api.smtp2go.com/v3/email/send",
        }
    )
    assert settings.is_delivering and settings.sender == SENDER
    assert settings.smtp2go_url.startswith("https://eu-api")


def test_mail_settings_from_empty_environment_is_not_delivering() -> None:
    assert not mail_settings({}).is_delivering


class FakeResponse(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_post_returns_answer_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: FakeResponse(b"ok"))
    assert post(urllib.request.Request("https://api.test/"), 1.0) == b"ok"


def test_post_http_error_raises_with_provider_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"data": {"error": "Sender not verified"}}).encode()

    def refuse(request: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.HTTPError(request.full_url, 400, "Bad", {}, io.BytesIO(body))  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(MailDeliveryError, match="400: Sender not verified"):
        post(urllib.request.Request("https://api.test/"), 1.0)


def test_post_http_error_with_plain_body_keeps_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.HTTPError(request.full_url, 502, "Bad", {}, io.BytesIO(b"gateway"))  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(MailDeliveryError, match="502: gateway"):
        post(urllib.request.Request("https://api.test/"), 1.0)


def test_post_unreachable_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(request: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    with pytest.raises(MailDeliveryError, match="Cannot reach"):
        post(urllib.request.Request("https://api.test/"), 1.0)


def test_background_mailer_sends_through_inner_mailer() -> None:
    transport = RecordingTransport()
    background = BackgroundMailer(Smtp2GoMailer(DELIVERING, transport))
    background.send("ana@example.com", "Verify", "Body")
    background.close()
    assert sent_payload(transport)["to"] == ["ana@example.com"]


def test_background_mailer_failure_is_logged_without_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = Smtp2GoMailer(DELIVERING, RecordingTransport(b"{}"))
    background = BackgroundMailer(failing)
    with caplog.at_level(logging.ERROR, logger="sreditor.mail"):
        background.send("ana@example.com", "Verify", "secret link")
        background.close()
    assert "Email to ana@example.com (Verify) was not sent" in caplog.text
    assert "secret link" not in caplog.text


def app_settings_with(base: AppSettings, mail: MailSettings) -> AppSettings:
    return base.model_copy(update={"mail": mail})


def test_build_mailer_with_api_key_sends_in_background(app_settings: AppSettings) -> None:
    mailer = build_mailer(app_settings_with(app_settings, DELIVERING))
    assert isinstance(mailer, BackgroundMailer)
    assert isinstance(mailer.mailer, Smtp2GoMailer)
    mailer.close()


def test_build_mailer_without_api_key_logs(app_settings: AppSettings) -> None:
    assert isinstance(build_mailer(app_settings), LogMailer)


def test_app_shutdown_stops_the_mail_workers(app_settings: AppSettings) -> None:
    app = create_app(app_settings_with(app_settings, DELIVERING))
    mailer = app.state.services.mailer
    with TestClient(app):
        pass
    with pytest.raises(RuntimeError, match="shutdown"):
        mailer.send("ana@example.com", "Verify", "Body")
