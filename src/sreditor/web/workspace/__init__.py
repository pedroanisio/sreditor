"""The signed-in workspace: server-rendered pages under /app (rule web.app.layout)."""

from __future__ import annotations

from fastapi import FastAPI, Request, Response, status

from sreditor.deps import services
from sreditor.i18n import N_
from sreditor.web.routes import Notice, message, redirect
from sreditor.web.workspace import (
    account,
    brand_kit,
    channels,
    classification,
    editorial,
    episode_srstudio,
    episodes,
    external_media,
    item_context,
    library,
    media,
    members,
    oauth,
    pitches,
    platforms,
    research,
    screenplay,
    search,
    short_script,
    srstudio,
    waivers,
)
from sreditor.web.workspace.shell import (
    APP_PATH,
    PageForbidden,
    PageNotFound,
    SignInRequired,
    sign_in_url,
)

ROUTERS = (
    channels.router,
    platforms.router,
    brand_kit.router,
    editorial.router,
    episodes.router,
    research.router,
    pitches.router,
    screenplay.router,
    short_script.router,
    media.router,
    waivers.router,
    library.router,
    srstudio.router,
    episode_srstudio.router,
    external_media.router,
    classification.router,
    item_context.router,
    members.router,
    account.router,
    oauth.router,
    search.router,
)

NOT_FOUND = Notice(
    title=N_("Not found"),
    text=N_("This page does not exist, or you are not a member of its channel."),
    status_code=status.HTTP_404_NOT_FOUND,
    link=(APP_PATH, N_("Your channels")),
)
FORBIDDEN = Notice(
    title=N_("Not allowed"),
    text=N_("Your role on this channel does not allow this. Ask an owner if you need it."),
    status_code=status.HTTP_403_FORBIDDEN,
    link=(APP_PATH, N_("Your channels")),
)


async def sign_in_first(request: Request, error: Exception) -> Response:
    """Send a visitor without a session to sign in, and bring them back afterwards."""
    next_path = error.next_path if isinstance(error, SignInRequired) else APP_PATH
    # redirect() keeps a ?lang= choice, so /app?lang=pt-BR signs in in that language too.
    return redirect(request, sign_in_url(next_path))


async def not_found(request: Request, error: Exception) -> Response:
    """The 404 page for a channel, member or file the person cannot see."""
    return message(request, services(request), NOT_FOUND)


async def forbidden(request: Request, error: Exception) -> Response:
    """The 403 page for an action the person's role does not allow."""
    return message(request, services(request), FORBIDDEN)


def mount(app: FastAPI) -> None:
    """Serve the workspace pages; they stay out of the API's OpenAPI document."""
    for router in ROUTERS:
        app.include_router(router, include_in_schema=False)
    app.add_exception_handler(SignInRequired, sign_in_first)
    app.add_exception_handler(PageNotFound, not_found)
    app.add_exception_handler(PageForbidden, forbidden)
