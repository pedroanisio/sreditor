"""The instance's render workers, for its admins (rule project.render.workers).

A worker's secret is shown once, on the page that answers its registration, and never stored or
shown again, like an API token's; that page is not cached. People who are not instance admins
get 403, and the link to the page is hidden from them.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Form, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.deps import Services, ServicesDep
from sreditor.render_operations import NotAdmin, list_workers, register_worker, revoke_worker
from sreditor.renders import NewWorker, WorkerName, WorkerNotFound
from sreditor.users import User
from sreditor.web.forms import SubmittedFields, field_errors
from sreditor.web.routes import rejected_form, translator
from sreditor.web.workspace.shell import (
    APP_PATH,
    Flash,
    Outcome,
    PageForbidden,
    PageNotFound,
    PageUser,
    done,
    workspace_page,
)

router = APIRouter()

WORKERS_PATH = f"{APP_PATH}/admin/render-workers"
# A page with a secret on it must not stay in any cache.
NO_STORE = {"Cache-Control": "no-store"}


class WorkerFields(SubmittedFields):
    """The registration form: the machine's name."""

    name: str = ""


class NameCheck(BaseModel):
    """A worker name, validated as the store keeps it."""

    model_config = ConfigDict(frozen=True)

    name: WorkerName


class WorkersView(BaseModel):
    """The workers page: errors and values of the form sent, and a secret just created."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome = Outcome()
    sent: WorkerFields | None = None
    created: NewWorker | None = None


def admin_only(user: User) -> None:
    """Refuse anyone but an instance admin, as the API does."""
    if not user.is_admin:
        raise PageForbidden(WORKERS_PATH)


def workers_page(request: Request, app: Services, user: User, shown: WorkersView) -> Response:
    """Every registered worker and the form that registers another."""
    context: dict[str, Any] = {
        "workers": list_workers(app, user),
        "errors": shown.outcome.errors,
        "sent": shown.sent,
        "created": shown.created,
        "action": WORKERS_PATH,
        "engine_version": app.settings.render.engine_version,
    }
    response = workspace_page(
        request,
        app,
        user,
        "workspace/render_workers.html",
        status_code=shown.outcome.status_code,
        **context,
    )
    response.headers.update(NO_STORE)
    return response


@router.get(WORKERS_PATH)
def workers(request: Request, app: ServicesDep, user: PageUser) -> Response:
    """The render workers: name, engine, GPUs and when each last asked for work."""
    admin_only(user)
    return workers_page(request, app, user, WorkersView())


@router.post(WORKERS_PATH)
def register(
    request: Request, app: ServicesDep, user: PageUser, fields: Annotated[WorkerFields, Form()]
) -> Response:
    """Register a GPU machine and show its secret, this once."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    admin_only(user)
    try:
        checked = NameCheck(name=fields.name)
        created = register_worker(app, user, checked.name)
    except ValidationError as invalid:
        outcome = Outcome.invalid(field_errors(invalid, translator(request)))
        return workers_page(request, app, user, WorkersView(outcome=outcome, sent=fields))
    except NotAdmin as refused:
        raise PageForbidden(WORKERS_PATH) from refused
    return workers_page(request, app, user, WorkersView(created=created))


@router.post(f"{WORKERS_PATH}/{{worker_id}}/revoke")
def revoke(
    request: Request,
    app: ServicesDep,
    user: PageUser,
    worker_id: UUID,
    fields: Annotated[SubmittedFields, Form()],
) -> Response:
    """Revoke a worker; it can claim no more jobs, and a job it held returns to the queue."""
    if rejected := rejected_form(request, app, fields.csrf_token):
        return rejected
    admin_only(user)
    try:
        revoke_worker(app, user, worker_id)
    except WorkerNotFound as unknown:
        raise PageNotFound(str(worker_id)) from unknown
    return done(request, app, WORKERS_PATH, Flash.REVOKED)
