"""Errors as RFC 9457 problem details (rule agentic.errors.problem).

`detail` keeps the same human-readable English message the API always sent (i18n.api.english),
so existing clients that read it keep working. Problem types that an agent can act on get a
stable URN; everything else is `about:blank`, whose meaning is just the HTTP status.
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

MEDIA_TYPE = "application/problem+json"
TYPE_PREFIX = "urn:sreditor:problem:"


def problem_type(name: str) -> str:
    """The full problem type URI for a short problem name."""
    return TYPE_PREFIX + name


class ProblemError(Exception):
    """An error with a specific problem type and optional extension members."""

    def __init__(
        self,
        status: int,
        detail: str,
        type_name: str | None = None,
        headers: dict[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.type = problem_type(type_name) if type_name else "about:blank"
        self.headers = headers
        self.extensions = extensions


def problem_response(
    request: Request,
    status: int,
    detail: str,
    type_uri: str = "about:blank",
    headers: Mapping[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    """Build an RFC 9457 problem response for the request, with any extension members."""
    body: dict[str, Any] = {
        "type": type_uri,
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        **extensions,
    }
    return JSONResponse(body, status, headers=headers, media_type=MEDIA_TYPE)


async def problem_error(request: Request, error: Exception) -> JSONResponse:
    """Answer a ProblemError with its own type, headers and extensions."""
    assert isinstance(error, ProblemError)
    return problem_response(
        request, error.status, error.detail, error.type, error.headers, **error.extensions
    )


async def http_error(request: Request, error: Exception) -> JSONResponse:
    """Answer any other HTTP error as a problem, keeping its message as the detail."""
    assert isinstance(error, StarletteHTTPException)
    return problem_response(request, error.status_code, str(error.detail), headers=error.headers)


async def validation_error(request: Request, error: Exception) -> JSONResponse:
    """Answer a validation failure with a problem that lists each invalid field."""
    assert isinstance(error, RequestValidationError)
    errors = [
        {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
        for error in error.errors()
    ]
    return problem_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "Request validation failed",
        problem_type("validation"),
        errors=errors,
    )


def install(app: FastAPI) -> None:
    """Register the handlers that turn every JSON error into a problem."""
    app.add_exception_handler(ProblemError, problem_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
