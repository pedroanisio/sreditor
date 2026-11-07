"""Public pages, rendered on the server with Jinja2 and enhanced with htmx (web.public.server-rendered).

Every page goes through `page` or `redirect`, which set the CSRF cookie and the security headers.
Opening a link that carries a token only renders a form; the token is spent on that form's POST,
so email link scanners cannot use it up (web.tokens.post-only).

Pages speak the visitor's language (i18n.negotiation). Messages are passed around as English
msgids marked with N_ and translated where they are shown: `message` translates its title, text
and link label, and form errors are translated when they are built.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.accounts import AccountError
from sreditor.channels import Platform
from sreditor.deps import (
    ERROR_STATUS,
    LANG_COOKIE,
    SESSION_COOKIE,
    Services,
    ServicesDep,
    request_locale,
    retry_after_seconds,
    services,
    session_token,
    start_session,
    within_rate_limit,
)
from sreditor.fountain import blocks
from sreditor.i18n import BASE_LOCALE, LOCALE_NAMES, LOCALES, N_, Translator, match_locale
from sreditor.members import Role
from sreditor.render import PRIVACY_CATEGORY, TERMS_CATEGORY
from sreditor.users import User
from sreditor.web.forms import (
    AFTER_SIGN_IN,
    CSRF_FIELD,
    EmailForm,
    FieldErrors,
    LoginFields,
    LoginForm,
    RegisterFields,
    RegisterForm,
    ResetForm,
    csrf_token,
    field_errors,
    is_csrf_valid,
    is_same_origin,
    set_csrf_cookie,
    token_from,
)
from sreditor.web.labels import platform_name
from sreditor.web.markdown_text import add_markdown_filters
from sreditor.web.static_assets import STATIC_DIR, STATIC_ROUTE, VersionedStaticFiles, static_url

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=HERE / "templates")
TEMPLATES.env.globals["static_url"] = static_url
add_markdown_filters(TEMPLATES.env)

# Where a signed-in visitor lands.
APP_PATH = AFTER_SIGN_IN

SECURITY_HEADERS = {
    # blob: lets the 3D viewer read the textures packed inside a model file: three.js hands them
    # to the browser as blob URLs that only this page's own scripts can make.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
        "connect-src 'self' blob:; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; object-src 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # No Referer leaves the site. Within it the browser must still send a real Origin on form
    # posts for the CSRF check; "no-referrer" makes it send "null" when JavaScript is off.
    "Referrer-Policy": "same-origin",
}

LANG_COOKIE_SECONDS = 365 * 24 * 60 * 60


INVITATIONS_PATH = "/invitations"
# Holds an opened invitation's token while its person signs in or signs up.
INVITATION_COOKIE = "sreditor_invitation"
INVITATION_COOKIE_SECONDS = 24 * 60 * 60

TOO_MANY = status.HTTP_429_TOO_MANY_REQUESTS
# workspace.js listens for this event to put the fresh token in the forms.
RENEWED_EVENT = "sreditor-form-renewed"
FORM_RENEWED = N_("This form had expired, so it was renewed. Submit it again.")
UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT
# How a browser submits a ticked checkbox.
CHECKED = "on"

TOO_MANY_ATTEMPTS = N_("Too many attempts. Wait a minute and try again.")
SIGN_IN = N_("Sign in")

FormField = Annotated[str, Form()]
CsrfField = Annotated[str, Form(alias=CSRF_FIELD)]
NextField = Annotated[str, Form(alias="next")]
NextQuery = Annotated[str, Query(alias="next")]

router = APIRouter(include_in_schema=False)


def mount(app: FastAPI) -> None:
    """Serve the public pages and their vendored static assets (web.security.headers)."""
    app.mount(f"/{STATIC_ROUTE}", VersionedStaticFiles(directory=STATIC_DIR), name=STATIC_ROUTE)
    app.include_router(router)


def translator(request: Request) -> Translator:
    """A translator for the language this request negotiated."""
    return Translator(request_locale(request))


def switch_links(request: Request) -> list[tuple[str, str, bool]]:
    """(href, name, current) for each language: this same page with ?lang= set."""
    current = request_locale(request)
    links = []
    for locale in LOCALES:
        url = request.url.include_query_params(lang=locale)
        links.append((f"{url.path}?{url.query}", LOCALE_NAMES[locale], locale == current))
    return links


def page(
    request: Request,
    app: Services,
    template: str,
    status_code: int = status.HTTP_200_OK,
    **context: Any,
) -> HTMLResponse:
    """Render a template with the shared context, CSRF cookie and security headers."""
    token = csrf_token(request)
    translate = translator(request)
    # The header shows who is signed in on every page.
    context.setdefault("user", signed_in(request, app))
    response = TEMPLATES.TemplateResponse(
        request,
        template,
        {
            "csrf_token": token,
            "csrf_field": CSRF_FIELD,
            "_": translate,
            "locale": translate.locale,
            "languages": switch_links(request),
            **context,
        },
        status_code,
    )
    set_csrf_cookie(response, token, app.settings.has_secure_cookies)
    remember_language(request, app, response)
    response.headers.update(SECURITY_HEADERS)
    if status_code == TOO_MANY:
        # Pages answer 429 too, so they say when to retry as the API does (agentic.rate-limits).
        response.headers["Retry-After"] = str(retry_after_seconds(request, app))
    return response


def remember_language(request: Request, app: Services, response: Response) -> None:
    """Keep a ?lang= choice in a cookie, so the next pages use it too (i18n.negotiation)."""
    if chosen := match_locale(request.query_params.get("lang")):
        response.set_cookie(
            LANG_COOKIE,
            chosen,
            max_age=LANG_COOKIE_SECONDS,
            httponly=True,
            secure=app.settings.has_secure_cookies,
            samesite="lax",
        )


class Notice(BaseModel):
    """What a message page says: English msgids, and an optional (href, label) link onward."""

    model_config = ConfigDict(frozen=True)

    title: str
    text: str
    status_code: int = status.HTTP_200_OK
    link: tuple[str, str] | None = None


def message(request: Request, app: Services, notice: Notice) -> HTMLResponse:
    """A page with one translated message and its status icon."""
    translate = translator(request)
    shown_link = (notice.link[0], translate(notice.link[1])) if notice.link else None
    return page(
        request,
        app,
        "message.html",
        notice.status_code,
        title=translate(notice.title),
        text=translate(notice.text),
        link=shown_link,
        tone=tone(notice.status_code, notice.link),
    )


def tone(status_code: int, link: tuple[str, str] | None) -> str:
    """The message's status icon: a refusal is an error, a finished step with a next link a success."""
    if status_code >= status.HTTP_400_BAD_REQUEST:
        return "error"
    return "success" if link else "info"


def redirect(request: Request, url: str) -> Response:
    """See Other for plain forms; for htmx requests, HX-Redirect makes htmx load the page."""
    response: Response
    if request.headers.get("hx-request"):
        response = Response(headers={"HX-Redirect": url})
    else:
        response = RedirectResponse(url, status.HTTP_303_SEE_OTHER)
    response.headers.update(SECURITY_HEADERS)
    # A ?lang= choice on a link that redirects (such as / for a signed-in visitor) would
    # otherwise be lost before any page could remember it (i18n.negotiation).
    remember_language(request, services(request), response)
    return response


def signed_in(request: Request, app: Services) -> User | None:
    """The user of this request's session, if any."""
    token = session_token(request)
    return app.users.session_user(token) if token else None


def safe_next(target: str) -> str:
    """Only follow a local path after sign-in, never another site.

    Browsers drop tabs and line breaks from a URL before reading it, so "/<tab>/evil.com" would
    become "//evil.com", another site; any whitespace or control character is refused.
    """
    is_plain = target.isprintable() and not any(character.isspace() for character in target)
    local = target.startswith("/") and not target.startswith("//") and "\\" not in target
    return target if local and is_plain else APP_PATH


def renewed_form(request: Request, app: Services) -> Response:
    """For an htmx form from our own pages: keep the page and what was typed, and hand the form
    a fresh token with a message to show above it (web.forms.errors). The token is the one the
    cookie set here holds, which is already readable from any of our forms."""
    token = csrf_token(request)
    detail = {"token": token, "message": translator(request)(FORM_RENEWED)}
    response = Response(
        status_code=status.HTTP_403_FORBIDDEN,
        headers={"HX-Reswap": "none", "HX-Trigger": json.dumps({RENEWED_EVENT: detail})},
    )
    set_csrf_cookie(response, token, app.settings.has_secure_cookies)
    response.headers.update(SECURITY_HEADERS)
    return response


def rejected_form(request: Request, app: Services, submitted: str) -> Response | None:
    """The answer when the CSRF check fails, or None when it passes.

    A form from our own pages sent by htmx gets its token renewed in place; anything else, such
    as a post from another site or a browser without JavaScript, gets the expired-form page.
    """
    if is_csrf_valid(request, submitted, app.settings.public_url):
        return None
    if request.headers.get("hx-request") and is_same_origin(request, app.settings.public_url):
        return renewed_form(request, app)
    expired = Notice(
        title=N_("This form has expired"),
        text=N_("Go back, reload the page and submit the form again."),
        status_code=status.HTTP_403_FORBIDDEN,
    )
    return message(request, app, expired)


def refusal_status(refusal: AccountError) -> int:
    """The HTTP status for an account refusal, as the JSON API uses it."""
    return ERROR_STATUS.get(type(refusal), status.HTTP_400_BAD_REQUEST)


@router.get("/")
def landing(request: Request, app: ServicesDep) -> Response:
    """The public landing page; a signed-in visitor goes to the app instead."""
    if signed_in(request, app):
        return redirect(request, APP_PATH)
    translate = translator(request)
    platforms = [platform_name(platform) for platform in Platform]
    roles = translate.any_of([translate.role(role) for role in Role])
    return page(request, app, "landing.html", platforms=platforms, roles=roles)


# Registration


def register_page(
    request: Request,
    app: Services,
    errors: FieldErrors | None = None,
    values: dict[str, str] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The registration form with any errors and the values already typed."""
    return page(
        request, app, "register.html", status_code, errors=errors or {}, values=values or {}
    )


@router.get("/register")
def register_form(request: Request, app: ServicesDep) -> Response:
    """An empty registration form."""
    return register_page(request, app)


@router.post("/register")
def register(
    request: Request, app: ServicesDep, fields: Annotated[RegisterFields, Form()]
) -> Response:
    """Create the account and send the confirmation email, or show the form's errors."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    translate = translator(request)
    values = {"email": fields.email, "display_name": fields.display_name}
    if not within_rate_limit(request, app):
        errors = {"form": translate(TOO_MANY_ATTEMPTS)}
        return register_page(request, app, errors, values, TOO_MANY)
    try:
        form = RegisterForm(
            email=fields.email,
            display_name=fields.display_name,
            password=fields.password,
            consent=fields.consent == CHECKED,
        )
    except ValidationError as invalid:
        return register_page(request, app, field_errors(invalid, translate), values, UNPROCESSABLE)
    app.accounts(translate.locale).register(form.email, form.display_name, form.password)
    return redirect(request, "/register/sent")


@router.get("/register/sent")
def register_sent(request: Request, app: ServicesDep) -> Response:
    """The same answer whether or not the email had an account (web.auth.no-enumeration)."""
    sent = Notice(
        title=N_("Check your email"),
        text=N_("We sent you a link to confirm your email address. It expires in two days."),
    )
    return message(request, app, sent)


@router.get("/verify")
def verify_form(request: Request, app: ServicesDep, token: str = "") -> Response:
    """Ask to confirm the email; opening the link spends nothing (web.tokens.post-only).

    New links carry the token in the fragment, which token.js copies into the form; `token` in
    the query string only comes from links sent before that.
    """
    return page(request, app, "verify.html", token=token, errors={})


@router.post("/verify")
def verify(
    request: Request, app: ServicesDep, csrf: CsrfField = "", token: FormField = ""
) -> Response:
    """Spend the verification token and say whether it worked."""
    if rejected := rejected_form(request, app, csrf):
        return rejected
    translate = translator(request)
    if not within_rate_limit(request, app):
        errors = {"form": translate(TOO_MANY_ATTEMPTS)}
        return page(request, app, "verify.html", TOO_MANY, token=token, errors=errors)
    try:
        app.accounts().verify_email(token_from(token))
    except AccountError as refusal:
        errors = {"token": translate(str(refusal))}
        return page(
            request, app, "verify.html", refusal_status(refusal), token=token, errors=errors
        )
    verified = Notice(
        title=N_("Email verified"), text=N_("Your account is ready."), link=("/login", SIGN_IN)
    )
    return message(request, app, verified)


# Sign in and out


class LoginView(BaseModel):
    """What the sign-in page shows: where to go next, the typed email and any errors."""

    model_config = ConfigDict(frozen=True)

    next_path: str
    email: str = ""
    errors: FieldErrors = {}
    status_code: int = status.HTTP_200_OK


def login_page(request: Request, app: Services, view: LoginView) -> HTMLResponse:
    """The sign-in form, keeping the email the visitor typed."""
    return page(
        request,
        app,
        "login.html",
        view.status_code,
        errors=view.errors,
        email=view.email,
        next_path=view.next_path,
    )


@router.get("/login")
def login_form(request: Request, app: ServicesDep, next_path: NextQuery = APP_PATH) -> Response:
    """The sign-in form; a visitor already signed in goes straight on."""
    if signed_in(request, app):
        return redirect(request, safe_next(next_path))
    return login_page(request, app, LoginView(next_path=safe_next(next_path)))


@router.post("/login")
def login(request: Request, app: ServicesDep, fields: Annotated[LoginFields, Form()]) -> Response:
    """Sign in and go on to the page the visitor came for."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    translate = translator(request)
    view = LoginView(next_path=safe_next(fields.next_path), email=fields.email)
    if not within_rate_limit(request, app):
        limited = {"errors": {"form": translate(TOO_MANY_ATTEMPTS)}, "status_code": TOO_MANY}
        return login_page(request, app, view.model_copy(update=limited))
    try:
        form = LoginForm(email=fields.email, password=fields.password)
        user = app.accounts().authenticate(form.email, form.password)
    except ValidationError as invalid:
        not_valid = {"errors": field_errors(invalid, translate), "status_code": UNPROCESSABLE}
        return login_page(request, app, view.model_copy(update=not_valid))
    except AccountError as refusal:
        refused = {
            "errors": {"form": translate(str(refusal))},
            "status_code": refusal_status(refusal),
        }
        return login_page(request, app, view.model_copy(update=refused))
    response = redirect(request, view.next_path)
    start_session(app, user, request, response)
    return response


@router.post("/logout")
def logout(
    request: Request, app: ServicesDep, csrf: CsrfField = "", next_path: NextField = ""
) -> Response:
    """Sign out, then go to `next` (a local path only) or the landing page."""
    if rejected := rejected_form(request, app, csrf):
        return rejected
    token = session_token(request)
    if token:
        app.users.delete_session(token)
    response = redirect(request, safe_next(next_path) if next_path else "/")
    response.delete_cookie(SESSION_COOKIE, httponly=True, secure=app.settings.has_secure_cookies)
    return response


# Password reset


@router.get("/forgot-password")
def forgot_form(request: Request, app: ServicesDep) -> Response:
    """Ask for the email to send a reset link to."""
    return page(request, app, "forgot.html", errors={}, email="")


@router.post("/forgot-password")
def forgot(
    request: Request, app: ServicesDep, csrf: CsrfField = "", email: FormField = ""
) -> Response:
    """Send a reset link, with the same answer whether or not the email has an account."""
    if rejected := rejected_form(request, app, csrf):
        return rejected
    translate = translator(request)
    if not within_rate_limit(request, app):
        errors = {"form": translate(TOO_MANY_ATTEMPTS)}
        return page(request, app, "forgot.html", TOO_MANY, errors=errors, email=email)
    try:
        form = EmailForm(email=email)
    except ValidationError as invalid:
        errors = field_errors(invalid, translate)
        return page(request, app, "forgot.html", UNPROCESSABLE, errors=errors, email=email)
    app.accounts(translate.locale).request_password_reset(form.email)
    sent = Notice(
        title=N_("Check your email"),
        text=N_(
            "If an account uses that email, we sent it a link to choose a new password. "
            "The link expires in one hour."
        ),
    )
    return message(request, app, sent)


@router.get("/reset-password")
def reset_form(request: Request, app: ServicesDep, token: str = "") -> Response:
    """Ask for a new password; opening the link spends nothing (see verify_form)."""
    return page(request, app, "reset.html", token=token, errors={})


@router.post("/reset-password")
def reset(
    request: Request,
    app: ServicesDep,
    csrf: CsrfField = "",
    token: FormField = "",
    password: FormField = "",
) -> Response:
    """Spend the reset token and set the new password."""
    if rejected := rejected_form(request, app, csrf):
        return rejected
    translate = translator(request)
    if not within_rate_limit(request, app):
        errors = {"form": translate(TOO_MANY_ATTEMPTS)}
        return page(request, app, "reset.html", TOO_MANY, token=token, errors=errors)
    try:
        form = ResetForm(token=token_from(token), password=password)
        app.accounts().reset_password(form.token, form.password)
    except ValidationError as invalid:
        errors = field_errors(invalid, translate)
        return page(request, app, "reset.html", UNPROCESSABLE, token=token, errors=errors)
    except AccountError as refusal:
        errors = {"token": translate(str(refusal))}
        return page(request, app, "reset.html", refusal_status(refusal), token=token, errors=errors)
    changed = Notice(
        title=N_("Password changed"),
        text=N_("Every other session was signed out. Sign in with your new password."),
        link=("/login", SIGN_IN),
    )
    return message(request, app, changed)


# Invitations


def remember_invitation(response: Response, token: str, is_secure: bool) -> None:
    """Keep the opened invitation in a cookie, so no later URL has to carry its token."""
    response.set_cookie(
        INVITATION_COOKIE,
        token,
        max_age=INVITATION_COOKIE_SECONDS,
        path=INVITATIONS_PATH,
        httponly=True,
        secure=is_secure,
        samesite="lax",
    )


def forget_invitation(response: Response) -> None:
    """Drop the invitation cookie once it is accepted or refused."""
    response.delete_cookie(INVITATION_COOKIE, path=INVITATIONS_PATH)


def opening_page(
    request: Request, app: Services, errors: FieldErrors, status_code: int = status.HTTP_200_OK
) -> HTMLResponse:
    """The page that opens an invitation from its link, or asks for the link."""
    return page(request, app, "invitation_open.html", status_code, errors=errors)


@router.get(INVITATIONS_PATH)
def invitation_page(request: Request, app: ServicesDep) -> Response:
    """Show the opened invitation and the way to accept it for this visitor."""
    token = request.cookies.get(INVITATION_COOKIE, "")
    if not token:
        return opening_page(request, app, {})
    try:
        invitation = app.accounts().open_invitation(token)
    except AccountError as refusal:
        errors = {"token": translator(request)(str(refusal))}
        response = opening_page(request, app, errors, refusal_status(refusal))
        forget_invitation(response)
        return response
    role = translator(request).role(invitation.role)
    here = INVITATIONS_PATH
    return page(request, app, "invitation.html", invitation=invitation, role=role, here=here)


@router.get("/invitations/{token}")
def legacy_invitation_link(request: Request, app: ServicesDep, token: str) -> Response:
    """Links sent before tokens moved to the fragment: keep the token, drop it from the URL."""
    response = redirect(request, INVITATIONS_PATH)
    remember_invitation(response, token, app.settings.has_secure_cookies)
    return response


@router.post("/invitations/open")
def open_invitation(
    request: Request, app: ServicesDep, csrf: CsrfField = "", token: FormField = ""
) -> Response:
    """Check the invitation's token and remember it; opening spends nothing."""
    if rejected := rejected_form(request, app, csrf):
        return rejected
    translate = translator(request)
    if not within_rate_limit(request, app):
        return opening_page(request, app, {"form": translate(TOO_MANY_ATTEMPTS)}, TOO_MANY)
    token = token_from(token)
    try:
        app.accounts().open_invitation(token)
    except AccountError as refusal:
        return opening_page(
            request, app, {"token": translate(str(refusal))}, refusal_status(refusal)
        )
    response = redirect(request, INVITATIONS_PATH)
    remember_invitation(response, token, app.settings.has_secure_cookies)
    return response


@router.post("/invitations/join")
def accept_invitation(request: Request, app: ServicesDep, csrf: CsrfField = "") -> Response:
    """Accept the opened invitation as the signed-in user."""
    if rejected := rejected_form(request, app, csrf):
        return rejected
    user = signed_in(request, app)
    if user is None:
        return redirect(request, f"/login?next={INVITATIONS_PATH}")
    try:
        app.accounts().accept_invitation(request.cookies.get(INVITATION_COOKIE, ""), user)
    except AccountError as refusal:
        refused = Notice(
            title=N_("Could not accept"),
            text=str(refusal),
            status_code=refusal_status(refusal),
            link=(APP_PATH, N_("Go to your channels")),
        )
        return message(request, app, refused)
    response = redirect(request, APP_PATH)
    forget_invitation(response)
    return response


# Legal pages, generated from the stmt store (docs.generated.from.stmt, i18n.prose)


def legal_page(request: Request, app: Services, category: str, title: str) -> HTMLResponse:
    """The statements in the page's category for this locale, or the English ones if none."""
    locale = request_locale(request)
    statements = app.stmt.statements()
    sections = []
    if locale != BASE_LOCALE:
        localized = f"{category}.{locale.lower()}"
        sections = [item for item in statements if item.category == localized]
    if not sections:
        sections = [item for item in statements if item.category == category]
    # The same order as the generated documents (render._order): priority, then id.
    sections.sort(key=lambda section: (section.priority, section.id))
    return page(request, app, "legal.html", title=translator(request)(title), sections=sections)


@router.get("/terms")
def terms(request: Request, app: ServicesDep) -> Response:
    """The terms of service, from the stmt store."""
    return legal_page(request, app, TERMS_CATEGORY, N_("Terms of service"))


@router.get("/privacy")
def privacy(request: Request, app: ServicesDep) -> Response:
    """The privacy policy, from the stmt store."""
    return legal_page(request, app, PRIVACY_CATEGORY, N_("Privacy policy"))


# The design system page, development only (rule ui.design-system)

DESIGN_COLORS = (
    "bg",
    "surface",
    "text",
    "text-muted",
    "border",
    "accent",
    "accent-soft",
    "danger",
    "danger-soft",
    "success",
    "success-soft",
    "warning",
    "warning-soft",
    "focus",
)
DESIGN_SPACING = tuple(range(1, 9))
DESIGN_TONES = ("info", "success", "warning", "error")
# (class mirroring the state, label) for each button state shown on /design.
DESIGN_STATES = (
    ("", N_("Default")),
    ("is-hover", N_("Hover")),
    ("is-focus", N_("Focus")),
    ("is-active", N_("Pressed")),
    ("is-loading", N_("Loading")),
)


DESIGN_SCREENPLAY = "INT. KITCHEN - DAY\n\nANA\n(sighing)\nIs it Monday again?\n\nCUT TO:"


@router.get("/design")
def design(request: Request, app: ServicesDep) -> Response:
    """Every component in each state and theme; a 404 unless SREDITOR_DEV_PAGES=true."""
    if not app.settings.has_dev_pages:
        return Response(status_code=status.HTTP_404_NOT_FOUND, headers=SECURITY_HEADERS)
    translate = translator(request)
    states = [(state, translate(label)) for state, label in DESIGN_STATES]
    return page(
        request,
        app,
        "design.html",
        states=states,
        colors=DESIGN_COLORS,
        spacing=DESIGN_SPACING,
        tones=DESIGN_TONES,
        design_screenplay=blocks(DESIGN_SCREENPLAY),
    )
