"""Prometheus metrics: request counts and durations, served on their own port."""

from __future__ import annotations

import time
from enum import StrEnum

import prometheus_client
from prometheus_client import Counter, Histogram
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# A path that matched no route; labelling it by its template keeps the series count bounded
# however many channels, episodes or stray URLs there are.
UNMATCHED_ROUTE = "unmatched"
HTTP_SCOPE = "http"
RESPONSE_START = "http.response.start"
SERVER_ERROR = 500


class Label(StrEnum):
    """Labels on the request metrics."""

    METHOD = "method"
    ROUTE = "route"
    STATUS = "status"


REQUESTS = Counter(
    "sreditor_http_requests",
    "HTTP requests answered, by method, route template and status code.",
    [Label.METHOD, Label.ROUTE, Label.STATUS],
)
DURATION = Histogram(
    "sreditor_http_request_duration_seconds",
    "Time to answer an HTTP request, by method and route template.",
    [Label.METHOD, Label.ROUTE],
)


def route_template(scope: Scope) -> str:
    """The matched route's path template, such as /channels/{channel}."""
    route = scope.get("route")
    if isinstance(route, BaseRoute):
        return str(getattr(route, "path", UNMATCHED_ROUTE))
    return UNMATCHED_ROUTE


class MetricsMiddleware:
    """Counts and times every HTTP request; other ASGI scopes pass through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer the request, then record its route, status and duration."""
        if scope["type"] != HTTP_SCOPE:
            await self.app(scope, receive, send)
            return
        status_code = SERVER_ERROR
        started = time.perf_counter()

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == RESPONSE_START:
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            # The router writes the matched route into the scope it was given, so it is read
            # only after the request has been answered.
            route = route_template(scope)
            REQUESTS.labels(scope["method"], route, str(status_code)).inc()
            DURATION.labels(scope["method"], route).observe(time.perf_counter() - started)


def serve(host: str, port: int) -> None:
    """Expose /metrics on its own port, so the public tunnel never reaches it."""
    prometheus_client.start_http_server(port, addr=host)
