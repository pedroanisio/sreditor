"""The public forms as Pydantic models, their field errors, and CSRF protection (web.forms.csrf)."""

from __future__ import annotations

import re
import secrets
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError, field_validator

from sreditor.channels import HEX_PATTERN, LANGUAGE_PATTERN, SLUG_PATTERN
from sreditor.delivery import HASHTAG_PATTERN
from sreditor.i18n import N_, Translator
from sreditor.security import new_token
from sreditor.users import DisplayName, Password

CSRF_COOKIE = "sreditor_csrf"
CSRF_FIELD = "csrf_token"
# Where sign-in leads when the visitor came for no page in particular.
AFTER_SIGN_IN = "/app"

# Field name -> message shown next to that field; the key "form" holds a message for the whole form.
FieldErrors = dict[str, str]

Token = Annotated[str, Field(min_length=1, max_length=256)]
# Where a token sits in an email link, old (?token=, /invitations/<token>) or new (#token=).
TOKEN_IN_LINK = re.compile(r"(?:token=|/invitations/)([\w-]+)")


class SubmittedFields(BaseModel):
    """A form as posted; validation happens in the *Form models, so errors render on the page."""

    model_config = ConfigDict(frozen=True)

    csrf_token: str = ""


class RegisterFields(SubmittedFields):
    """The registration form as posted."""

    email: str = ""
    display_name: str = ""
    password: str = ""
    consent: str = ""


class LoginFields(SubmittedFields):
    """The sign-in form as posted; `next` is where to go afterwards."""

    email: str = ""
    password: str = ""
    next_path: str = Field(default=AFTER_SIGN_IN, alias="next")


class RegisterForm(BaseModel):
    """A valid registration: email, display name, password and consent."""

    model_config = ConfigDict(frozen=True)

    email: EmailStr
    display_name: DisplayName
    password: Password
    consent: bool

    @field_validator("consent")
    @classmethod
    def consent_given(cls, consent: bool) -> bool:
        """Registration needs explicit consent to the terms and privacy policy (web.signup.consent)."""
        if not consent:
            raise ValueError(N_("Accept the terms of service and privacy policy to continue."))
        return consent


class LoginForm(BaseModel):
    """A sign-in attempt with both fields filled in."""

    model_config = ConfigDict(frozen=True)

    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=256)]


class EmailForm(BaseModel):
    """A request that needs only a valid email address."""

    model_config = ConfigDict(frozen=True)

    email: EmailStr


class ResetForm(BaseModel):
    """A new password and the reset token that allows it."""

    model_config = ConfigDict(frozen=True)

    token: Token
    password: Password


URL_ERROR_PREFIX = "url_"
DATE_ERROR_PREFIX = "date_"
REQUIRED = N_("This field is required.")
# Pydantic's own text is English and technical, so an error without a message of ours gets this.
CHECK_VALUE = N_("Check this value.")
# Messages that need no numbers, by Pydantic error type.
FIXED_MESSAGES = {
    "missing": REQUIRED,
    "enum": N_("Choose one of the options."),
    "literal_error": N_("Choose one of the options."),
    "int_parsing": N_("Enter a whole number."),
    "int_type": N_("Enter a whole number."),
}
# Messages with one number from the error's context: type -> (context key, message).
COUNTED_MESSAGES = {
    "string_too_short": ("min_length", N_("Use at least %(count)s characters.")),
    "string_too_long": ("max_length", N_("Use at most %(count)s characters.")),
    "greater_than_equal": ("ge", N_("Use %(count)s or more.")),
    "less_than_equal": ("le", N_("Use %(count)s or less.")),
}
# A pattern says what shape a value must have; each one we use has its own message.
PATTERN_MESSAGES = {
    SLUG_PATTERN: N_("Use lowercase letters and numbers, with single hyphens between words."),
    HEX_PATTERN: N_("Enter a color as # and six hex digits, such as #ff0066."),
    LANGUAGE_PATTERN: N_("Enter a language code such as en or pt-BR."),
    HASHTAG_PATTERN: N_("A hashtag has letters, numbers and underscores only, with no spaces."),
}
PREFIX_MESSAGES = {
    URL_ERROR_PREFIX: N_("Enter a full link, starting with https://."),
    DATE_ERROR_PREFIX: N_("Enter a valid date."),
}


def message_for(error: dict[str, Any], translate: Translator) -> str:
    """A short, human message for one Pydantic error, in the translator's language."""
    kind = error["type"]
    context = error.get("ctx", {})
    if kind == "string_too_short" and context.get("min_length") == 1:
        return translate(REQUIRED)
    if kind in COUNTED_MESSAGES:
        key, message = COUNTED_MESSAGES[kind]
        return translate(message, count=context[key])
    if kind == "string_pattern_mismatch":
        return translate(PATTERN_MESSAGES.get(context.get("pattern", ""), CHECK_VALUE))
    if kind == "value_error":
        return value_error_message(error, translate)
    prefixed = [message for prefix, message in PREFIX_MESSAGES.items() if kind.startswith(prefix)]
    return translate(FIXED_MESSAGES.get(kind, prefixed[0] if prefixed else CHECK_VALUE))


def value_error_message(error: dict[str, Any], translate: Translator) -> str:
    """Our validators raise ValueError with an N_-marked message; email checks have their own."""
    if "email" in error["msg"]:
        return translate(N_("Enter a valid email address."))
    return translate(str(error.get("ctx", {}).get("error", CHECK_VALUE)))


def field_errors(invalid: ValidationError, translate: Translator) -> FieldErrors:
    """The first error for each field, keyed by field name."""
    errors: FieldErrors = {}
    for error in invalid.errors():
        name = str(error["loc"][0]) if error["loc"] else "form"
        errors.setdefault(name, message_for(dict(error), translate))
    return errors


def token_from(submitted: str) -> str:
    """The token in a form field: the token itself, or a whole email link pasted without JavaScript."""
    found = TOKEN_IN_LINK.search(submitted)
    return found.group(1) if found else submitted.strip()


def csrf_token(request: Request) -> str:
    """The visitor's CSRF token: the one in their cookie, or a new one."""
    return request.cookies.get(CSRF_COOKIE) or new_token()


def set_csrf_cookie(response: Response, token: str, is_secure: bool) -> None:
    """Store the CSRF token where only same-site requests send it back."""
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=is_secure, samesite="strict")


def origin_of(url: str) -> str:
    """The scheme and host of `url`, as browsers send it in the Origin header."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def is_same_origin(request: Request, public_url: str) -> bool:
    """Whether the browser says the request comes from our own pages."""
    return request.headers.get("origin") == origin_of(public_url)


def is_csrf_valid(request: Request, submitted: str, public_url: str) -> bool:
    """Double-submit check: the form's token matches the cookie, and Origin is our public URL."""
    cookie = request.cookies.get(CSRF_COOKIE, "")
    is_ours = is_same_origin(request, public_url)
    return is_ours and bool(cookie) and secrets.compare_digest(cookie, submitted)
