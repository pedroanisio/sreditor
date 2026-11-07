"""Screenplay routes: an episode's screenplay, in Fountain."""

from __future__ import annotations

from fastapi import APIRouter, Response

from sreditor import screenplay_operations as screenplays
from sreditor.deps import Access, ServicesDep
from sreditor.episode_routes import Number, WriteTarget
from sreditor.etags import etag
from sreditor.screenplay import Screenplay, ScreenplayInput

router = APIRouter(tags=["screenplay"])

SCREENPLAY = "/channels/{slug}/episodes/{number}/screenplay"


@router.get(SCREENPLAY, summary="Read the screenplay")
def get_screenplay(
    number: Number, access: Access, app: ServicesDep, response: Response
) -> Screenplay:
    """Read an episode's screenplay (Fountain) with the scenes and speaking characters in it.

    Revision 0 (ETag "0") means nobody wrote it yet.
    """
    found = screenplays.get_screenplay(app, access, number)
    response.headers["ETag"] = etag(found.revision)
    return found


@router.put(SCREENPLAY, summary="Write the screenplay")
def write_screenplay(
    target: WriteTarget,
    data: ScreenplayInput,
    access: Access,
    app: ServicesDep,
    response: Response,
) -> Screenplay:
    """Replace an episode's whole screenplay, written in Fountain (https://fountain.io).

    Needs content:write. Send If-Match with the ETag you read ("0" when it is empty) so a change
    you have not seen is never overwritten.
    """
    written = screenplays.write_screenplay(
        app, access, target.number, data, target.expected_revision
    )
    response.headers["ETag"] = etag(written.revision)
    return written
