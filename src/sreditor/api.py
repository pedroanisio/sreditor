"""HTTP API: channels, platform accounts, brand kits, Editorial, users, members and tokens."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.routing import Route

from sreditor import (
    auth_routes,
    book_routes,
    brand_routes,
    claim_routes,
    classification_routes,
    context_routes,
    delivery_routes,
    episode_routes,
    export_routes,
    external_media_routes,
    file_routes,
    library_routes,
    mcp_server,
    media_routes,
    member_routes,
    metrics,
    oauth_routes,
    operations,
    overview_routes,
    pitch_routes,
    post_metrics_routes,
    problems,
    render_routes,
    research_routes,
    screenplay_routes,
    search_routes,
    short_script_routes,
    srstudio_routes,
    token_routes,
    waiver_routes,
)
from sreditor.assets import AssetRejected, AssetStore, FileTooLarge
from sreditor.block_takes import BlockTakeStore
from sreditor.channels import Channel, Platform, PlatformAccount
from sreditor.claims import ClaimStore
from sreditor.classification import ClassificationStore
from sreditor.content_search import ContentSearchStore
from sreditor.context import ContextStore
from sreditor.database import Clock, Database, utc_now
from sreditor.delivery import DeliveryStore
from sreditor.deps import Access, CurrentUser, Services, ServicesDep, request_token
from sreditor.editorial import (
    CitedStatementNotFound,
    Editorial,
    EditorialService,
    Guideline,
    GuidelineChanged,
    GuidelineInput,
    GuidelineNotCited,
    GuidelineNotFound,
)
from sreditor.elevenlabs import build_voice
from sreditor.episode_texts import PartChanged
from sreditor.episodes import EpisodeChanged, EpisodeNotFound, EpisodeStore
from sreditor.etags import IfMatch, etag, expected_revision
from sreditor.external_media import ExternalMediaStore
from sreditor.file_links import FileLinkStore
from sreditor.idempotency import IdempotencyDep
from sreditor.library import EntryExists, EntryNotFound, LibraryStore, StyleNotInLibrary
from sreditor.mail import BackgroundMailer, LogMailer, Mailer, Smtp2GoMailer
from sreditor.media import MediaNotFound, MediaStore
from sreditor.members import MemberStore, Permission
from sreditor.pagination import DEFAULT_LIMIT, Cursor, Limit, Page
from sreditor.pitches import PitchStore
from sreditor.post_metrics import PostMetricsStore
from sreditor.problems import ProblemError, problem_response, problem_type
from sreditor.render_upload import PartStore
from sreditor.renders import RenderStore
from sreditor.repository import (
    AccountTaken,
    ChannelExists,
    ChannelNotFound,
    ChannelRepository,
    RevisionMismatch,
)
from sreditor.research import ResearchStore, SourceNotFound
from sreditor.screenplay import ScreenplayStore
from sreditor.search import SearchStore
from sreditor.security import Passwords, RateLimiter
from sreditor.settings import AppSettings
from sreditor.short_script import ShortScriptStore
from sreditor.srstudio import SrstudioStore
from sreditor.store import StmtClient, StoreError
from sreditor.tokens import TokenStore
from sreditor.users import UserStore
from sreditor.voice_generation import TakeStore
from sreditor.waivers import WaiverNotFound, WaiverStore
from sreditor.web import routes as web_routes
from sreditor.web import workspace

# stmt error codes that describe the request rather than the store's health.
STORE_STATUS = {"CONFLICT": status.HTTP_409_CONFLICT}

# Sign-in, registration and reset attempts allowed per client and endpoint.
AUTH_ATTEMPTS = 10
AUTH_WINDOW_SECONDS = 60.0
AGENT_WINDOW_SECONDS = 60.0


def operation_id(route: APIRoute) -> str:
    """Stable snake_case operationIds from the route function names (agentic.openapi.described)."""
    return route.name


def with_etag(revised: operations.Revised, response: Response) -> Channel:
    """Set the ETag header from the channel's revision; the channel is the response body."""
    response.headers["ETag"] = etag(revised.revision)
    return revised.channel


def build_mailer(settings: AppSettings) -> Mailer:
    """SMTP2GO, sending beside each request, once an API key is set; otherwise the log."""
    if settings.mail.is_delivering:
        return BackgroundMailer(Smtp2GoMailer(settings.mail))
    return LogMailer(is_revealing_tokens=settings.has_tokens_in_mail_log)


DOCS_PATH = "/docs"
REDOC_PATH = "/redoc"


def create_app(
    settings: AppSettings,
    client: StmtClient | None = None,
    mailer: Mailer | None = None,
    passwords: Passwords | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """Build the FastAPI app, creating each collaborator from settings unless one is passed in."""
    client = client or StmtClient(settings.store)
    database = Database(settings.channels_db, clock)
    users = UserStore(database)
    assets = AssetStore(settings.assets_dir, settings.max_upload_bytes)
    services = Services(
        settings=settings,
        channels=ChannelRepository(database),
        episodes=EpisodeStore(database),
        research=ResearchStore(database),
        pitches=PitchStore(database),
        screenplays=ScreenplayStore(database),
        short_scripts=ShortScriptStore(database),
        takes=TakeStore(database),
        block_takes=BlockTakeStore(database),
        library=LibraryStore(database),
        media=MediaStore(database, assets, settings.max_media_bytes),
        external_media=ExternalMediaStore(database, assets, settings.max_media_bytes),
        classifications=ClassificationStore(database),
        waivers=WaiverStore(database, assets, settings.max_media_bytes),
        search=SearchStore(database),
        content_search=ContentSearchStore(database),
        contexts=ContextStore(database),
        srstudio=SrstudioStore(database),
        users=users,
        members=MemberStore(database),
        editorials=EditorialService(client, settings.owner),
        passwords=passwords or Passwords(),
        mailer=mailer or build_mailer(settings),
        limiter=RateLimiter(AUTH_ATTEMPTS, AUTH_WINDOW_SECONDS),
        agent_limiter=RateLimiter(settings.agent_requests_per_minute, AGENT_WINDOW_SECONDS),
        stmt=client,
        tokens=TokenStore(database, users),
        assets=assets,
        renders=RenderStore(database),
        render_parts=PartStore(settings.assets_dir, settings.max_media_bytes),
        deliveries=DeliveryStore(database),
        post_metrics=PostMetricsStore(database),
        file_links=FileLinkStore(database),
        claims=ClaimStore(database),
        voice=build_voice(settings.voice),
    )
    mcp = mcp_server.build(services)
    mcp_app = mcp_server.asgi_app(mcp, services)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield
        if isinstance(services.mailer, BackgroundMailer):
            services.mailer.close()

    app = FastAPI(
        title="sreditor",
        version="0.1.0",
        description="Manage social media channels, their platform accounts, brand kits and "
        "Editorial guidelines, and produce their episodes. Agents authenticate with personal "
        "API tokens (`sre_...`).",
        lifespan=lifespan,
        generate_unique_id_function=operation_id,
        # Swagger UI and ReDoc load scripts from a CDN and send none of the security headers
        # (rule web.security.headers), so they exist only in development. /openapi.json stays.
        docs_url=DOCS_PATH if settings.has_dev_pages else None,
        redoc_url=REDOC_PATH if settings.has_dev_pages else None,
    )
    app.state.services = services
    app.add_middleware(metrics.MetricsMiddleware)
    problems.install(app)
    install_error_handlers(app)
    install_production_error_handlers(app)
    install_media_error_handlers(app)
    srstudio_routes.install_srstudio_error_handlers(app)
    render_routes.install_error_handlers(app)
    file_routes.install_error_handlers(app)
    pitch_routes.install_error_handlers(app)
    claim_routes.install_error_handlers(app)
    context_routes.install_error_handlers(app)
    external_media_routes.install_error_handlers(app)
    short_script_routes.install_short_script_error_handlers(app)
    classification_routes.install_error_handlers(app)
    delivery_routes.install_error_handlers(app)
    post_metrics_routes.install_error_handlers(app)
    app.include_router(auth_routes.router)
    app.include_router(token_routes.router)
    app.include_router(oauth_routes.router)
    app.include_router(member_routes.router)
    app.include_router(episode_routes.router)
    app.include_router(overview_routes.router)
    app.include_router(research_routes.router)
    app.include_router(pitch_routes.router)
    app.include_router(claim_routes.router)
    app.include_router(brand_routes.router)
    app.include_router(context_routes.router)
    app.include_router(screenplay_routes.router)
    app.include_router(short_script_routes.router)
    app.include_router(library_routes.router)
    app.include_router(media_routes.router)
    app.include_router(waiver_routes.router)
    app.include_router(external_media_routes.router)
    app.include_router(classification_routes.router)
    app.include_router(delivery_routes.router)
    app.include_router(post_metrics_routes.router)
    app.include_router(srstudio_routes.router)
    app.include_router(render_routes.router)
    app.include_router(render_routes.worker_router)
    app.include_router(export_routes.router)
    app.include_router(book_routes.router)
    app.include_router(file_routes.router)
    app.include_router(search_routes.router)
    add_channel_routes(app)
    app.router.routes.append(Route(mcp_server.PATH, mcp_app, methods=["GET", "POST", "DELETE"]))
    workspace.mount(app)
    web_routes.mount(app)
    return app


def install_error_handlers(app: FastAPI) -> None:
    """Domain errors as problem details; `detail` keeps the messages clients already know."""

    @app.exception_handler(ChannelNotFound)
    async def channel_not_found(request: Request, missing: ChannelNotFound) -> JSONResponse:
        return problem_response(request, status.HTTP_404_NOT_FOUND, f"Channel {missing} not found")

    @app.exception_handler(ChannelExists)
    async def channel_exists(request: Request, duplicate: ChannelExists) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_409_CONFLICT,
            f"Channel {duplicate} already exists",
            problem_type("channel-exists"),
        )

    @app.exception_handler(AccountTaken)
    async def account_taken(request: Request, taken: AccountTaken) -> JSONResponse:
        return problem_response(
            request, status.HTTP_409_CONFLICT, str(taken), problem_type("account-taken")
        )

    @app.exception_handler(GuidelineChanged)
    async def guideline_changed(request: Request, changed: GuidelineChanged) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_412_PRECONDITION_FAILED,
            f"Guideline {changed} changed since you read it; read it again and retry",
            problem_type("stale-revision"),
            headers={"ETag": etag(changed.current)},
        )

    @app.exception_handler(GuidelineNotFound)
    async def guideline_not_found(request: Request, missing: GuidelineNotFound) -> JSONResponse:
        return problem_response(
            request, status.HTTP_404_NOT_FOUND, f"Guideline {missing} not found"
        )

    @app.exception_handler(CitedStatementNotFound)
    async def cited_statement_not_found(
        request: Request, missing: CitedStatementNotFound
    ) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Statement {missing} does not exist or was retired; cite an enabled statement id",
            problem_type("cited-statement-not-found"),
        )

    @app.exception_handler(GuidelineNotCited)
    async def guideline_not_cited(request: Request, uncited: GuidelineNotCited) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_409_CONFLICT,
            f"Guideline {uncited} cites no statement; set cited_statement_id when writing it",
            problem_type("guideline-not-cited"),
        )

    @app.exception_handler(RevisionMismatch)
    async def revision_mismatch(request: Request, mismatch: RevisionMismatch) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_412_PRECONDITION_FAILED,
            f"Channel {mismatch} changed since you read it; read it again and retry",
            problem_type("stale-revision"),
            headers={"ETag": etag(mismatch.current)},
        )

    @app.exception_handler(StoreError)
    async def store_error(request: Request, error: StoreError) -> JSONResponse:
        code = STORE_STATUS.get(error.code or "", status.HTTP_502_BAD_GATEWAY)
        return problem_response(request, code, str(error))


def install_production_error_handlers(app: FastAPI) -> None:
    """Errors of episodes, their parts and the channel library, as problem details."""

    @app.exception_handler(EpisodeNotFound)
    async def episode_not_found(request: Request, missing: EpisodeNotFound) -> JSONResponse:
        return problem_response(request, status.HTTP_404_NOT_FOUND, f"Episode {missing} not found")

    @app.exception_handler(EpisodeChanged)
    async def episode_changed(request: Request, changed: EpisodeChanged) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_412_PRECONDITION_FAILED,
            f"Episode {changed} changed since you read it; read it again and retry",
            problem_type("stale-revision"),
            headers={"ETag": etag(changed.current)},
        )

    @app.exception_handler(SourceNotFound)
    async def source_not_found(request: Request, missing: SourceNotFound) -> JSONResponse:
        return problem_response(request, status.HTTP_404_NOT_FOUND, f"Source {missing} not found")

    @app.exception_handler(PartChanged)
    async def part_changed(request: Request, changed: PartChanged) -> JSONResponse:
        return problem_response(
            request,
            status.HTTP_412_PRECONDITION_FAILED,
            f"{changed} changed since you read it; read it again and retry",
            problem_type("stale-revision"),
            headers={"ETag": etag(changed.current)},
        )

    @app.exception_handler(EntryExists)
    async def entry_exists(request: Request, taken: EntryExists) -> JSONResponse:
        return problem_response(
            request, status.HTTP_409_CONFLICT, str(taken), problem_type("entry-exists")
        )

    @app.exception_handler(EntryNotFound)
    async def entry_not_found(request: Request, missing: EntryNotFound) -> JSONResponse:
        return problem_response(
            request, status.HTTP_404_NOT_FOUND, f"Library entry {missing} not found"
        )


def install_media_error_handlers(app: FastAPI) -> None:
    """Errors of episode media, waivers and styles; a refused value names its field."""

    @app.exception_handler(MediaNotFound)
    async def media_not_found(request: Request, missing: MediaNotFound) -> JSONResponse:
        return problem_response(request, status.HTTP_404_NOT_FOUND, f"Media {missing} not found")

    @app.exception_handler(WaiverNotFound)
    async def waiver_not_found(request: Request, missing: WaiverNotFound) -> JSONResponse:
        return problem_response(request, status.HTTP_404_NOT_FOUND, f"Waiver {missing} not found")

    @app.exception_handler(StyleNotInLibrary)
    async def style_not_in_library(request: Request, refused: StyleNotInLibrary) -> JSONResponse:
        message = str(refused)
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            message,
            problem_type("unknown-style"),
            errors=[
                {"location": ["body", "style_id"], "message": message, "type": "unknown-style"}
            ],
        )

    @app.exception_handler(AssetRejected)
    async def file_rejected(request: Request, rejection: AssetRejected) -> JSONResponse:
        name = "file-too-large" if isinstance(rejection, FileTooLarge) else "unsupported-file"
        message = str(rejection)
        return problem_response(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            message,
            problem_type(name),
            errors=[{"location": ["body", "file"], "message": message, "type": name}],
        )


def add_channel_routes(app: FastAPI) -> None:
    """Channel, platform account, brand kit and Editorial routes."""
    add_service_routes(app)
    add_channel_record_routes(app)
    add_account_routes(app)
    add_brand_and_editorial_routes(app)


def add_service_routes(app: FastAPI) -> None:
    """Routes about the service itself rather than any channel."""

    @app.get("/health", tags=["service"], summary="Check health")
    def health() -> dict[str, str]:
        """Liveness check: answers `ok` while the service runs."""
        return {"status": "ok"}


def add_channel_record_routes(app: FastAPI) -> None:
    """List, create, read, replace and delete channels."""

    @app.get("/channels", tags=["channels"], summary="List channels")
    def list_channels(
        request: Request,
        user: CurrentUser,
        services: ServicesDep,
        cursor: Cursor = None,
        limit: Limit = DEFAULT_LIMIT,
    ) -> Page[Channel]:
        """List the channels you belong to (an admin sees all), ordered by slug."""
        return operations.list_channels(services, user, cursor, limit, request_token(request))

    @app.post(
        "/channels",
        status_code=status.HTTP_201_CREATED,
        response_model=Channel,
        tags=["channels"],
        summary="Create a channel",
    )
    def create_channel(
        channel: Channel,
        request: Request,
        user: CurrentUser,
        services: ServicesDep,
        idempotency: IdempotencyDep,
    ) -> Response:
        """Create a channel; you become its owner. Accepts an Idempotency-Key header."""
        token = request_token(request)
        return idempotency.run(
            lambda: operations.create_channel(services, user, token, channel),
            status.HTTP_201_CREATED,
        )

    @app.get("/channels/{slug}", tags=["channels"], summary="Get a channel")
    def get_channel(access: Access, response: Response) -> Channel:
        """Read a channel. Its ETag is the revision to send as If-Match when changing it."""
        access.require(Permission.READ)
        response.headers["ETag"] = etag(access.revision)
        return access.channel

    @app.put("/channels/{slug}", tags=["channels"], summary="Replace a channel")
    def replace_channel(
        channel: Channel,
        access: Access,
        services: ServicesDep,
        response: Response,
        if_match: IfMatch = None,
    ) -> Channel:
        """Replace the whole channel document. Send If-Match to avoid overwriting others."""
        revised = operations.replace_channel(services, access, channel, expected_revision(if_match))
        return with_etag(revised, response)

    @app.delete(
        "/channels/{slug}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["channels"],
        summary="Delete a channel",
    )
    def delete_channel(access: Access, services: ServicesDep, if_match: IfMatch = None) -> Response:
        """Delete the record, its members and invitations. The Editorial stays in stmt."""
        operations.delete_channel(services, access, expected_revision(if_match))
        return Response(status_code=status.HTTP_204_NO_CONTENT)


def account_for_path(platform: Platform, account: PlatformAccount) -> PlatformAccount:
    """The account in the body, refused when it names another platform than the path."""
    if account.platform != platform:
        raise ProblemError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Platform in body and path differ"
        )
    return account


AccountForPath = Annotated[PlatformAccount, Depends(account_for_path)]


def add_account_routes(app: FastAPI) -> None:
    """Add, replace and remove a channel's account on each platform."""

    @app.put(
        "/channels/{slug}/accounts/{platform}",
        tags=["channels"],
        summary="Add or replace an account",
    )
    def put_account(
        account: AccountForPath,
        access: Access,
        services: ServicesDep,
        response: Response,
        if_match: IfMatch = None,
    ) -> Channel:
        """Add or replace the channel's account on one platform."""
        revised = operations.put_account(services, access, account, expected_revision(if_match))
        return with_etag(revised, response)

    @app.delete(
        "/channels/{slug}/accounts/{platform}", tags=["channels"], summary="Remove an account"
    )
    def delete_account(
        platform: Platform,
        access: Access,
        services: ServicesDep,
        response: Response,
        if_match: IfMatch = None,
    ) -> Channel:
        """Remove the channel's account on one platform; returns the updated channel."""
        revised = operations.delete_account(services, access, platform, expected_revision(if_match))
        return with_etag(revised, response)


def add_brand_and_editorial_routes(app: FastAPI) -> None:
    """Read the brand kit and Editorial, and write or retire guidelines."""

    @app.get("/channels/{slug}/editorial", tags=["editorial"], summary="Get the Editorial")
    def get_editorial(
        access: Access, services: ServicesDep, platform: Platform | None = None
    ) -> Editorial:
        """House rules, the channel's guidelines and, with a platform, its overrides.

        `warnings` lists each guideline that cites a stmt statement whose text now differs
        from the guideline's copy, or that cannot be checked: statement id, both revisions and
        both texts. Nothing is overwritten; call take_cited_text to adopt the statement's text.
        """
        return operations.read_editorial_checked(services, access, platform)

    @app.post(
        "/channels/{slug}/editorial",
        status_code=status.HTTP_201_CREATED,
        response_model=Guideline,
        tags=["editorial"],
        summary="Write a guideline",
    )
    def write_guideline(
        data: GuidelineInput,
        access: Access,
        services: ServicesDep,
        idempotency: IdempotencyDep,
        if_match: IfMatch = None,
    ) -> Response:
        """Set the purpose, voice or audience, or add a pillar or an avoid guideline.

        Accepts an Idempotency-Key header, so a retried pillar is not added twice. For purpose,
        voice and audience send If-Match with the guideline's revision from get_editorial ("0"
        when it is not set yet): a text someone changed since is answered with 412.
        """
        expected = expected_revision(if_match)
        return idempotency.run(
            lambda: operations.write_guideline(services, access, data, expected),
            status.HTTP_201_CREATED,
        )

    @app.post(
        "/channels/{slug}/editorial/{statement_id}/take-cited-text",
        response_model=Guideline,
        tags=["editorial"],
        summary="Take the cited statement's text",
    )
    def take_cited_text(
        statement_id: str,
        access: Access,
        services: ServicesDep,
        platform: Platform | None = None,
        if_match: IfMatch = None,
    ) -> Guideline:
        """Revise a guideline to the current text of the stmt statement it cites.

        Use it when get_editorial shows a `changed` warning for the guideline and you want the
        statement's text. It is an ordinary revision recorded under your name; it needs
        editorial:write and answers 409 when the guideline cites nothing and 422 when the cited
        statement is gone. Send the guideline's `revision` as If-Match to refuse the change
        when someone revised it since you read it.
        """
        revision = expected_revision(if_match)
        return operations.take_cited_text(services, access, statement_id, platform, revision)

    @app.delete(
        "/channels/{slug}/editorial/{statement_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["editorial"],
        summary="Retire a guideline",
    )
    def retire_guideline(
        statement_id: str,
        access: Access,
        services: ServicesDep,
        platform: Platform | None = None,
        if_match: IfMatch = None,
    ) -> Response:
        """Retire one of the channel's guidelines by its statement id.

        Send the guideline's `revision` from get_editorial as If-Match ("3") to refuse the
        change when someone revised it since you read it.
        """
        revision = expected_revision(if_match)
        operations.retire_guideline(services, access, statement_id, platform, revision)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
