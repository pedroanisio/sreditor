"""The Cloudflare client that creates the tunnel, routes the hostname and returns its token."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any, Self

import pytest
from pydantic import SecretStr

from sreditor.cloudflare import (
    Cloudflare,
    CloudflareError,
    TunnelPlan,
    ensure_tunnel,
    send,
    zone_candidates,
)

API = "https://api.cloudflare.test/client/v4"
PLAN = TunnelPlan(name="sreditor-production", hostname="app.example.com", service="http://s:8000")
ZONE = {"id": "zone-1", "account": {"id": "acct-1"}}


class FakeApi:
    """Answers each `METHOD /path` (query ignored) with a fixed result and records the calls."""

    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        path = request.full_url.removeprefix(API).split("?")[0]
        body = json.loads(request.data) if isinstance(request.data, bytes) else None
        self.calls.append((request.get_method(), path, body))
        result = self.results.get(f"{request.get_method()} {path}")
        return json.dumps({"success": True, "errors": [], "result": result}).encode()

    def call_keys(self) -> list[str]:
        return [f"{method} {path}" for method, path, _ in self.calls]


def new_account() -> FakeApi:
    return FakeApi(
        {
            "GET /zones": [ZONE],
            "GET /accounts/acct-1/cfd_tunnel": [],
            "POST /accounts/acct-1/cfd_tunnel": {"id": "tun-1"},
            "GET /zones/zone-1/dns_records": [],
            "GET /accounts/acct-1/cfd_tunnel/tun-1/token": "tunnel-token",
        }
    )


def client(api: FakeApi) -> Cloudflare:
    return Cloudflare(SecretStr("cf-api-token"), API, api)


def test_ensure_tunnel_in_new_account_returns_token() -> None:
    assert ensure_tunnel(client(new_account()), PLAN) == "tunnel-token"


def test_ensure_tunnel_in_new_account_creates_tunnel_managed_by_cloudflare() -> None:
    api = new_account()
    ensure_tunnel(client(api), PLAN)
    created = [body for method, path, body in api.calls if method == "POST" and "cfd" in path]
    assert created == [{"name": "sreditor-production", "config_src": "cloudflare"}]


def test_ensure_tunnel_routes_hostname_and_refuses_the_rest() -> None:
    api = new_account()
    ensure_tunnel(client(api), PLAN)
    [config] = [body for method, _, body in api.calls if method == "PUT"]
    assert config == {
        "config": {
            "ingress": [
                {"hostname": "app.example.com", "service": "http://s:8000"},
                {"service": "http_status:404"},
            ]
        }
    }


def test_ensure_tunnel_without_dns_record_creates_proxied_cname() -> None:
    api = new_account()
    ensure_tunnel(client(api), PLAN)
    [record] = [
        body
        for method, path, body in api.calls
        if (method, path) == ("POST", "/zones/zone-1/dns_records")
    ]
    assert record == {
        "type": "CNAME",
        "name": "app.example.com",
        "content": "tun-1.cfargotunnel.com",
        "proxied": True,
    }


def test_ensure_tunnel_with_existing_tunnel_and_record_updates_them() -> None:
    api = new_account()
    api.results["GET /accounts/acct-1/cfd_tunnel"] = [{"id": "tun-1"}]
    api.results["GET /zones/zone-1/dns_records"] = [{"id": "rec-1"}]
    ensure_tunnel(client(api), PLAN)
    assert "POST /accounts/acct-1/cfd_tunnel" not in api.call_keys()
    assert "PUT /zones/zone-1/dns_records/rec-1" in api.call_keys()


def test_zone_for_hostname_without_zone_raises() -> None:
    api = new_account()
    api.results["GET /zones"] = []
    with pytest.raises(CloudflareError, match="No zone for app.example.com"):
        client(api).zone_for("app.example.com")


def test_zone_candidates_list_parent_domains_longest_first() -> None:
    assert zone_candidates("a.b.example.com") == ["b.example.com", "example.com"]


def test_call_sends_api_token_as_bearer() -> None:
    api = new_account()
    seen: list[urllib.request.Request] = []

    def recording(request: urllib.request.Request, timeout: float) -> bytes:
        seen.append(request)
        return api(request, timeout)

    Cloudflare(SecretStr("cf-api-token"), API, recording).call("GET", "/zones")
    assert seen[0].get_header("Authorization") == "Bearer cf-api-token"


def test_call_refused_raises_with_cloudflare_messages() -> None:
    def refuse(request: urllib.request.Request, timeout: float) -> bytes:
        return json.dumps(
            {"success": False, "errors": [{"message": "Authentication error"}]}
        ).encode()

    with pytest.raises(CloudflareError, match="GET /zones: Authentication error"):
        Cloudflare(SecretStr("bad"), API, refuse).call("GET", "/zones")


def test_call_unreadable_answer_raises() -> None:
    with pytest.raises(CloudflareError, match="unreadable"):
        Cloudflare(SecretStr("t"), API, lambda request, timeout: b"<html>").call("GET", "/zones")


class FakeResponse(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_send_returns_answer_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: FakeResponse(b"ok"))
    assert send(urllib.request.Request(API), 1.0) == b"ok"


def test_send_http_error_returns_its_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.HTTPError(API, 403, "Forbidden", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert send(urllib.request.Request(API), 1.0) == b"{}"


def test_send_unreachable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(request: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    with pytest.raises(CloudflareError, match="Cannot reach Cloudflare"):
        send(urllib.request.Request(API), 1.0)
