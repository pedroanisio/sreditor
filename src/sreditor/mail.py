"""Outgoing email: SMTP2GO in production, the log in development and tests."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

from sreditor.redact import redact

MAIL_LOGGER = "sreditor.mail"
logger = logging.getLogger(MAIL_LOGGER)

DEFAULT_SMTP2GO_URL = "https://api.smtp2go.com/v3/email/send"
API_KEY_HEADER = "X-Smtp2go-Api-Key"
JSON_TYPE = "application/json"
SECURE_SCHEME = "https://"
SEND_TIMEOUT_SECONDS = 15.0
# Sending runs beside the request, so a slow provider never delays a response.
SENDER_THREADS = 2

Transport = Callable[[urllib.request.Request, float], bytes]


class Mailer(Protocol):
    """Anything that can send an email."""

    def send(self, to: str, subject: str, body: str) -> None:
        """Send a plain-text email to one address."""


class MailDeliveryError(RuntimeError):
    """The provider did not accept an email."""


class MailSettings(BaseModel):
    """How email leaves the service. Without an API key, emails only go to the log."""

    model_config = ConfigDict(frozen=True)

    smtp2go_api_key: SecretStr | None = None
    smtp2go_url: str = DEFAULT_SMTP2GO_URL
    sender: str | None = None  # "Name <address@your-domain>", a sender verified in SMTP2GO

    @model_validator(mode="after")
    def sender_required_with_key(self) -> Self:
        """Require a verified sender and an https URL whenever an API key is set."""
        if self.smtp2go_api_key is not None and (self.sender is None or "@" not in self.sender):
            raise ValueError("SREDITOR_MAIL_SENDER must name a verified sender address")
        if not self.smtp2go_url.startswith(SECURE_SCHEME):
            # The API key travels in a header, so it must never go over plain HTTP.
            raise ValueError("SREDITOR_SMTP2GO_URL must be an https:// URL")
        return self

    @property
    def is_delivering(self) -> bool:
        """Whether emails go out through SMTP2GO; without an API key they only go to the log."""
        return self.smtp2go_api_key is not None


class LogMailer:
    """Writes each email to the log, with tokens redacted.

    `is_revealing_tokens` keeps them, so a developer can follow the links locally. Never enable it
    where other people can read the logs.
    """

    def __init__(self, is_revealing_tokens: bool = False) -> None:
        self.is_revealing_tokens = is_revealing_tokens

    def send(self, to: str, subject: str, body: str) -> None:
        """Log the email, with tokens redacted unless `is_revealing_tokens` is set."""
        text = body if self.is_revealing_tokens else redact(body)
        logger.warning("Email to %s: %s\n%s", to, subject, text)


def post(request: urllib.request.Request, timeout: float) -> bytes:
    """Send the request; HTTP errors keep their body, which names the provider's reason."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: bytes = response.read()
            return body
    except urllib.error.HTTPError as refusal:
        raise MailDeliveryError(
            f"SMTP2GO answered {refusal.code}: {provider_error(refusal.read())}"
        ) from refusal
    except (urllib.error.URLError, TimeoutError) as failure:
        raise MailDeliveryError(f"Cannot reach SMTP2GO: {failure}") from failure


def provider_error(body: bytes) -> str:
    """The `data.error` message of an SMTP2GO answer, or the raw answer when it has none."""
    try:
        data = json.loads(body).get("data", {})
    except (ValueError, AttributeError):
        return body.decode(errors="replace")[:200]
    return str(data.get("error") or data.get("failures") or data)


class Smtp2GoMailer:
    """Sends plain-text email through the SMTP2GO HTTP API."""

    def __init__(self, settings: MailSettings, transport: Transport = post) -> None:
        if settings.smtp2go_api_key is None or settings.sender is None:
            raise ValueError("Smtp2GoMailer needs an API key and a sender")
        self.url = settings.smtp2go_url
        self.api_key = settings.smtp2go_api_key
        self.sender = settings.sender
        self.transport = transport

    def send(self, to: str, subject: str, body: str) -> None:
        """Send the email through the SMTP2GO API; raise MailDeliveryError if it is not accepted."""
        payload = {"sender": self.sender, "to": [to], "subject": subject, "text_body": body}
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": JSON_TYPE,
                "Accept": JSON_TYPE,
                API_KEY_HEADER: self.api_key.get_secret_value(),
            },
            method="POST",
        )
        answer = self.transport(request, SEND_TIMEOUT_SECONDS)
        check_accepted(answer)


def check_accepted(answer: bytes) -> None:
    """SMTP2GO answers 200 even when an email failed, so read `data.succeeded`."""
    try:
        data: dict[str, Any] = json.loads(answer).get("data", {})
    except (ValueError, AttributeError) as invalid:
        raise MailDeliveryError("SMTP2GO sent an unreadable answer") from invalid
    if data.get("failed") or not data.get("succeeded"):
        raise MailDeliveryError(f"SMTP2GO did not send the email: {provider_error(answer)}")


class BackgroundMailer:
    """Sends through another mailer on worker threads, logging any failure.

    Requests answer at once, and the same way whether or not an email went out, so response
    times never reveal which emails have accounts (web.auth.no-enumeration).
    """

    def __init__(self, mailer: Mailer, threads: int = SENDER_THREADS) -> None:
        self.mailer = mailer
        self.executor = ThreadPoolExecutor(threads, thread_name_prefix="mail")

    def send(self, to: str, subject: str, body: str) -> None:
        """Queue the email for a worker thread, so the request does not wait for the provider."""
        future = self.executor.submit(self.mailer.send, to, subject, body)
        future.add_done_callback(lambda done: report(done, to, subject))

    def close(self) -> None:
        """Wait for queued emails, then stop the workers."""
        self.executor.shutdown(wait=True)


def report(done: Future[None], to: str, subject: str) -> None:
    """Log a failed send; the request has already been answered, so nothing else can handle it."""
    failure = done.exception()
    if failure is not None:
        logger.error("Email to %s (%s) was not sent: %s", to, subject, failure)
