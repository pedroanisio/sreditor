"""Static assets with content-hashed URLs, so a deploy never serves stale CSS or JavaScript.

Pages link each asset as /static/<path>?v=<hash of its content>. A new deploy changes the hash
and so the URL, which lets browsers and the Cloudflare edge keep a versioned URL forever. A
request without the version must revalidate, since its content can change on the next deploy.
"""

from __future__ import annotations

import hashlib
from functools import cache
from os import PathLike, stat_result
from pathlib import Path

from jinja2 import pass_context
from jinja2.runtime import Context
from starlette.datastructures import QueryParams
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR = Path(__file__).parent / "static"
STATIC_ROUTE = "static"
VERSION_PARAM = "v"
# Enough hex digits that two versions of one file never collide in practice.
VERSION_LENGTH = 12
CACHE_VERSIONED = "public, max-age=31536000, immutable"
CACHE_UNVERSIONED = "no-cache"


@cache
def asset_version(path: str) -> str:
    """A short hash of the asset's bytes; files only change between deploys, so it is cached."""
    content = (STATIC_DIR / path).read_bytes()
    return hashlib.sha256(content).hexdigest()[:VERSION_LENGTH]


@pass_context
def static_url(context: Context, path: str) -> str:
    """The asset's absolute URL with its content version, for templates."""
    url = context["request"].url_for(STATIC_ROUTE, path=path)
    return f"{url}?{VERSION_PARAM}={asset_version(path)}"


class VersionedStaticFiles(StaticFiles):
    """Static files cached forever when requested by version, revalidated otherwise."""

    def file_response(
        self,
        full_path: str | PathLike[str],
        stat_result: stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        """Serve the file with a Cache-Control that matches whether the URL is versioned."""
        response = super().file_response(full_path, stat_result, scope, status_code)
        query = QueryParams(scope.get("query_string", b""))
        is_versioned = VERSION_PARAM in query
        response.headers["Cache-Control"] = CACHE_VERSIONED if is_versioned else CACHE_UNVERSIONED
        return response
