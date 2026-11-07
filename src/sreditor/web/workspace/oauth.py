"""The OAuth consent page: the signed-in person lets an MCP client act for them (agentic.oauth).

Approving creates a single-use code and sends the browser back to the client, which trades it
for an ordinary API token; the person picks which permissions that token gets.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import Field

from sreditor.deps import Services, ServicesDep
from sreditor.i18n import N_
from sreditor.mcp_server import PATH as MCP_PATH
from sreditor.oauth import (
    AuthorizationRequest,
    ErrorCode,
    Grant,
    OAuthClient,
    OAuthError,
    redirect_with,
    requested_scopes,
    resources_for,
)
from sreditor.oauth_routes import AUTHORIZE_PATH, base_url, oauth_store
from sreditor.tokens import Scope
from sreditor.users import User
from sreditor.web.forms import SubmittedFields
from sreditor.web.labels import SCOPE_HINTS
from sreditor.web.routes import SECURITY_HEADERS, Notice, message, rejected_form, translator
from sreditor.web.workspace.shell import PageUser, workspace_page

router = APIRouter()

DENY_PATH = AUTHORIZE_PATH + "/deny"
CHOOSE_SCOPE = N_("Choose at least one permission.")
INVALID_REQUEST = Notice(
    title=N_("This connection request is not valid"),
    text=N_("Start connecting again from the app that sent you here."),
    status_code=status.HTTP_400_BAD_REQUEST,
)
FORM_ACTION = "form-action 'self'"


class ConsentFields(SubmittedFields):
    """The consent form: the authorization request, carried in hidden fields, and the scopes."""

    client_id: str = ""
    redirect_uri: str = ""
    response_type: str = ""
    code_challenge: str = ""
    code_challenge_method: str = ""
    state: str = ""
    scope: str = ""
    resource: str = ""
    scopes: list[str] = Field(default_factory=list)

    def authorization(self) -> AuthorizationRequest:
        """The authorization request carried in the form's hidden fields."""
        return AuthorizationRequest.model_validate(
            self.model_dump(exclude={"csrf_token", "scopes"})
        )


def checked(app: Services, authorization: AuthorizationRequest) -> OAuthClient:
    """The client, when the request is valid; raises OAuthError otherwise."""
    resources = resources_for(base_url(app), MCP_PATH)
    return oauth_store(app).check(authorization, resources)


def to_client(request: Request, uri: str) -> Response:
    """Send the browser back to the client. Its origin joins form-action so the browser follows
    the redirect that answers this form (CSP applies form-action to redirects)."""
    if request.headers.get("hx-request"):
        response: Response = Response(headers={"HX-Redirect": uri})
    else:
        response = RedirectResponse(uri, status.HTTP_303_SEE_OTHER)
    response.headers.update(SECURITY_HEADERS)
    parts = urlsplit(uri)
    policy = response.headers["Content-Security-Policy"]
    widened = f"{FORM_ACTION} {parts.scheme}://{parts.netloc}"
    response.headers["Content-Security-Policy"] = policy.replace(FORM_ACTION, widened)
    return response


def refusal_answer(
    request: Request, app: Services, authorization: AuthorizationRequest, refusal: OAuthError
) -> Response:
    """Tell the client by redirect when that is safe; otherwise show the error here."""
    if not refusal.can_redirect:
        return message(request, app, INVALID_REQUEST)
    params = {"error": refusal.code.value, "error_description": refusal.description}
    return to_client(request, error_redirect(app, authorization, params))


def error_redirect(
    app: Services, authorization: AuthorizationRequest, params: dict[str, str]
) -> str:
    """The client's redirect URI carrying an error, its state and our issuer."""
    return redirect_with(
        authorization.redirect_uri, {**params, "state": authorization.state, "iss": base_url(app)}
    )


def consent_page(
    request: Request,
    app: Services,
    user: User,
    shown: tuple[OAuthClient, AuthorizationRequest, list[str]],
    errors: dict[str, str] | None = None,
) -> Response:
    """The client's name, where it will send the person, and the permissions to grant."""
    client, authorization, selected = shown
    translate = translator(request)
    context: dict[str, Any] = {
        "client": client,
        "client_host": urlsplit(authorization.redirect_uri).netloc,
        "authorization": authorization.model_dump(),
        "scope_choices": [
            # People read what a permission does; its scope id is the secondary hint.
            (scope.value, translate(SCOPE_HINTS[scope]), scope.value)
            for scope in Scope
        ],
        "selected": selected,
        "errors": errors or {},
    }
    status_code = status.HTTP_400_BAD_REQUEST if errors else status.HTTP_200_OK
    response = workspace_page(
        request, app, user, "workspace/oauth_consent.html", status_code=status_code, **context
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(AUTHORIZE_PATH, summary="Show the consent page")
def authorize(
    request: Request,
    app: ServicesDep,
    user: PageUser,
    query: Annotated[AuthorizationRequest, Query()],
) -> Response:
    """Ask the person whether the client may act for them, and with which permissions."""
    try:
        client = checked(app, query)
    except OAuthError as refusal:
        return refusal_answer(request, app, query, refusal)
    selected = [scope.value for scope in requested_scopes(query.scope)]
    return consent_page(request, app, user, (client, query, selected))


@router.post(AUTHORIZE_PATH, summary="Approve a connection")
def approve(
    request: Request, app: ServicesDep, user: PageUser, fields: Annotated[ConsentFields, Form()]
) -> Response:
    """Grant the chosen permissions and send the person back to the client with a code."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    authorization = fields.authorization()
    try:
        client = checked(app, authorization)
    except OAuthError as refusal:
        return refusal_answer(request, app, authorization, refusal)
    known = {scope.value: scope for scope in Scope}
    scopes = [known[name] for name in fields.scopes if name in known]
    if not scopes:
        errors = {"scopes": translator(request)(CHOOSE_SCOPE)}
        return consent_page(request, app, user, (client, authorization, []), errors)
    grant = Grant(
        client_id=client.client_id,
        user_id=user.id,
        redirect_uri=authorization.redirect_uri,
        code_challenge=authorization.code_challenge,
        scopes=scopes,
    )
    code = oauth_store(app).issue_code(grant)
    params = {"code": code, "state": authorization.state, "iss": base_url(app)}
    return to_client(request, redirect_with(authorization.redirect_uri, params))


@router.post(DENY_PATH, summary="Deny a connection")
def deny(
    request: Request, app: ServicesDep, user: PageUser, fields: Annotated[ConsentFields, Form()]
) -> Response:
    """Refuse: the client is told access_denied and gets no code."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    authorization = fields.authorization()
    try:
        checked(app, authorization)
    except OAuthError as refusal:
        return refusal_answer(request, app, authorization, refusal)
    params = {
        "error": ErrorCode.ACCESS_DENIED.value,
        "error_description": "The person refused",
    }
    return to_client(request, error_redirect(app, authorization, params))
