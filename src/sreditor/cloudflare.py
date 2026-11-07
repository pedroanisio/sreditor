"""A small Cloudflare API client that creates a tunnel and routes a hostname to it."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr

DEFAULT_API_URL = "https://api.cloudflare.com/client/v4"
REQUEST_TIMEOUT_SECONDS = 20.0
JSON_TYPE = "application/json"
# Dashboard-managed tunnels keep their routes in Cloudflare, so cloudflared needs only a token.
CONFIG_SOURCE = "cloudflare"
TUNNEL_DNS_SUFFIX = "cfargotunnel.com"
CATCH_ALL_SERVICE = "http_status:404"
CNAME = "CNAME"
MIN_ZONE_LABELS = 2

Transport = Callable[[urllib.request.Request, float], bytes]


class CloudflareError(Exception):
    """Cloudflare refused a request or could not be reached."""


class Zone(BaseModel):
    """The DNS zone that holds a hostname, and the account that owns it."""

    model_config = ConfigDict(frozen=True)

    id: str
    account_id: str


class TunnelPlan(BaseModel):
    """The tunnel to have: its name, the public hostname and the service it reaches."""

    model_config = ConfigDict(frozen=True)

    name: str
    hostname: str
    service: str


def send(request: urllib.request.Request, timeout: float) -> bytes:
    """POST/GET over HTTPS; an HTTP error still returns Cloudflare's JSON body."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: bytes = response.read()
            return body
    except urllib.error.HTTPError as refusal:
        return refusal.read()
    except urllib.error.URLError as unreachable:
        raise CloudflareError(f"Cannot reach Cloudflare: {unreachable.reason}") from unreachable


def zone_candidates(hostname: str) -> list[str]:
    """Parent domains of a hostname, longest first: a.b.example.com -> b.example.com, example.com."""
    labels = hostname.split(".")
    return [".".join(labels[start:]) for start in range(1, len(labels) - MIN_ZONE_LABELS + 1)]


class Cloudflare:
    """Calls the Cloudflare v4 API with an API token."""

    def __init__(self, api_token: SecretStr, base_url: str, transport: Transport = send) -> None:
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    def call(self, method: str, path: str, body: object | None = None) -> Any:
        """One API call; returns its `result` or raises with Cloudflare's error messages."""
        request = urllib.request.Request(
            self.base_url + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_token.get_secret_value()}",
                "Content-Type": JSON_TYPE,
            },
        )
        raw = self.transport(request, REQUEST_TIMEOUT_SECONDS)
        try:
            answer = json.loads(raw)
        except json.JSONDecodeError as unreadable:
            raise CloudflareError(f"{method} {path}: unreadable answer") from unreadable
        if not answer.get("success"):
            messages = "; ".join(error.get("message", "") for error in answer.get("errors", []))
            raise CloudflareError(f"{method} {path}: {messages or 'refused'}")
        return answer.get("result")

    def zone_for(self, hostname: str) -> Zone:
        """The zone in this account that the hostname belongs to."""
        for name in zone_candidates(hostname):
            found = self.call("GET", "/zones?" + urllib.parse.urlencode({"name": name}))
            if found:
                return Zone(id=found[0]["id"], account_id=found[0]["account"]["id"])
        raise CloudflareError(f"No zone for {hostname} in this Cloudflare account")

    def tunnel_id(self, account_id: str, name: str) -> str:
        """The id of the tunnel with this name, created when there is none."""
        query = urllib.parse.urlencode({"name": name, "is_deleted": "false"})
        found = self.call("GET", f"/accounts/{account_id}/cfd_tunnel?{query}")
        if found:
            return str(found[0]["id"])
        body = {"name": name, "config_src": CONFIG_SOURCE}
        return str(self.call("POST", f"/accounts/{account_id}/cfd_tunnel", body)["id"])

    def route(self, account_id: str, tunnel_id: str, plan: TunnelPlan) -> None:
        """Send the hostname to the service and refuse everything else."""
        ingress = [{"hostname": plan.hostname, "service": plan.service}]
        config = {"ingress": [*ingress, {"service": CATCH_ALL_SERVICE}]}
        path = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"
        self.call("PUT", path, {"config": config})

    def point_dns(self, zone_id: str, hostname: str, tunnel_id: str) -> None:
        """A proxied CNAME from the hostname to the tunnel, created or updated."""
        record = {
            "type": CNAME,
            "name": hostname,
            "content": f"{tunnel_id}.{TUNNEL_DNS_SUFFIX}",
            "proxied": True,
        }
        query = urllib.parse.urlencode({"name": hostname, "type": CNAME})
        found = self.call("GET", f"/zones/{zone_id}/dns_records?{query}")
        if found:
            self.call("PUT", f"/zones/{zone_id}/dns_records/{found[0]['id']}", record)
        else:
            self.call("POST", f"/zones/{zone_id}/dns_records", record)

    def tunnel_token(self, account_id: str, tunnel_id: str) -> str:
        """The token cloudflared runs the tunnel with."""
        return str(self.call("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token"))


def ensure_tunnel(cloudflare: Cloudflare, plan: TunnelPlan) -> str:
    """Create or update the tunnel, its route and its DNS record; return its token."""
    zone = cloudflare.zone_for(plan.hostname)
    tunnel_id = cloudflare.tunnel_id(zone.account_id, plan.name)
    cloudflare.route(zone.account_id, tunnel_id, plan)
    cloudflare.point_dns(zone.id, plan.hostname, tunnel_id)
    return cloudflare.tunnel_token(zone.account_id, tunnel_id)
