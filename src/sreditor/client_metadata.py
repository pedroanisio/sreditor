"""Client ID Metadata Documents (rule agentic.oauth.cimd).

An MCP client may use an https URL as its client_id; sreditor fetches the JSON document at
that URL to learn the client's name and redirect URIs. Fetching a URL a stranger chose is a
server-side request forgery risk, so the fetch connects only to public addresses (checked
after DNS resolution, and pinned so a second lookup cannot swap in a private one), follows no
redirects, and stops at a size and a time limit.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from sreditor.oauth import ClientRegistration, ErrorCode, OAuthClient, OAuthError

HTTPS = "https"
HTTPS_PORT = 443
FETCH_TIMEOUT_SECONDS = 5.0
MAX_DOCUMENT_BYTES = 16 * 1024
JSON_TYPE = "application/json"
USER_AGENT = "sreditor-oauth"
DEFAULT_CACHE_SECONDS = 5 * 60
MAX_CACHE_SECONDS = 24 * 60 * 60
MAX_CACHED_DOCUMENTS = 256
MAX_AGE = re.compile(r"max-age=(\d+)")
NO_STORE = "no-store"


class ClientDocumentError(Exception):
    """The client_id URL is not allowed, or its document cannot be used."""


class FetchedDocument(BaseModel):
    """A document's body and the seconds it may be cached."""

    model_config = ConfigDict(frozen=True)

    body: bytes
    cache_seconds: int


def is_client_id_url(client_id: str) -> bool:
    """CIMD client ids are https URLs with a path; registered ids are opaque strings."""
    parts = urlsplit(client_id)
    return parts.scheme == HTTPS and bool(parts.hostname) and parts.path not in ("", "/")


def check_url(url: str) -> None:
    """https with a path, no credentials, query fragment or dot segments (CIMD section 3)."""
    parts = urlsplit(url)
    if not is_client_id_url(url):
        raise ClientDocumentError("client_id must be an https URL with a path")
    if parts.username or parts.password or parts.fragment:
        raise ClientDocumentError("client_id URL must not carry credentials or a fragment")
    if any(segment in (".", "..") for segment in parts.path.split("/")):
        raise ClientDocumentError("client_id URL must not contain dot segments")


def is_public(address: str) -> bool:
    """Whether an IP is on the public internet: not private, loopback, link-local and so on."""
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    return parsed.is_global and not parsed.is_multicast


Resolver = Callable[[str, int], list[str]]


def resolve(host: str, port: int) -> list[str]:
    """Every address the host resolves to."""
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as unknown:
        raise ClientDocumentError(f"Cannot resolve {host}") from unknown
    return [str(info[4][0]) for info in found]


def public_address(host: str, port: int, resolver: Resolver = resolve) -> str:
    """One address to connect to, refused when any address of the host is not public."""
    addresses = resolver(host, port)
    if not addresses or not all(is_public(address) for address in addresses):
        raise ClientDocumentError(f"{host} does not resolve only to public addresses")
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a checked address, while TLS and Host still name the original host."""

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        """Open the socket to the pinned address, then TLS for the original host name."""
        raw = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)  # type: ignore[attr-defined]


def cache_seconds(cache_control: str) -> int:
    """How long the document may be kept, within sreditor's bounds."""
    if NO_STORE in cache_control:
        return 0
    found = MAX_AGE.search(cache_control)
    seconds = int(found.group(1)) if found else DEFAULT_CACHE_SECONDS
    return min(seconds, MAX_CACHE_SECONDS)


def fetch(url: str, resolver: Resolver = resolve) -> FetchedDocument:
    """GET the document from a public address, with no redirects, within the limits."""
    check_url(url)
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or HTTPS_PORT
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    connection = PinnedHTTPSConnection(
        host, port, public_address(host, port, resolver), FETCH_TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", target, headers={"Accept": JSON_TYPE, "User-Agent": USER_AGENT})
        response = connection.getresponse()
        body = response.read(MAX_DOCUMENT_BYTES + 1)
    except (OSError, http.client.HTTPException) as failed:
        raise ClientDocumentError(f"Cannot fetch {url}: {failed}") from failed
    finally:
        connection.close()
    if response.status != http.client.OK:
        raise ClientDocumentError(f"{url} answered {response.status}")
    if len(body) > MAX_DOCUMENT_BYTES:
        raise ClientDocumentError(f"{url} is larger than {MAX_DOCUMENT_BYTES} bytes")
    return FetchedDocument(
        body=body, cache_seconds=cache_seconds(response.getheader("Cache-Control", ""))
    )


class ClientDocument(ClientRegistration):
    """A metadata document: registration metadata plus the client_id it names itself by."""

    client_id: str


def client_from(url: str, body: bytes) -> OAuthClient:
    """The client a document describes, when it names itself by the URL it was fetched from."""
    try:
        document = ClientDocument.model_validate(json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as invalid:
        raise ClientDocumentError(f"{url} is not a valid client metadata document") from invalid
    if document.client_id != url:
        raise ClientDocumentError("The document's client_id does not match its URL")
    return OAuthClient(
        client_id=url,
        client_name=document.client_name,
        redirect_uris=document.redirect_uris,
        created_at=datetime.now(UTC).replace(microsecond=0),
    )


Fetcher = Callable[[str], FetchedDocument]
Clock = Callable[[], float]


@dataclass(frozen=True)
class ClientDocuments:
    """Fetches client documents and keeps each as long as its Cache-Control allows."""

    fetcher: Fetcher = fetch
    clock: Clock = time.monotonic
    cache: dict[str, tuple[float, OAuthClient]] = field(default_factory=dict)

    def client(self, url: str) -> OAuthClient:
        """The client behind a client_id URL; a document that fails is an invalid client."""
        cached = self.cache.get(url)
        if cached and cached[0] > self.clock():
            return cached[1]
        try:
            fetched = self.fetcher(url)
            client = client_from(url, fetched.body)
        except ClientDocumentError as refusal:
            raise OAuthError(ErrorCode.INVALID_CLIENT, str(refusal)) from refusal
        if fetched.cache_seconds:
            self.keep(url, (self.clock() + fetched.cache_seconds, client))
        return client

    def keep(self, url: str, entry: tuple[float, OAuthClient]) -> None:
        """Cache an entry; anyone can publish documents, so the cache stays bounded."""
        if len(self.cache) >= MAX_CACHED_DOCUMENTS:
            now = self.clock()
            for expired in [key for key, kept in self.cache.items() if kept[0] <= now]:
                del self.cache[expired]
        while len(self.cache) >= MAX_CACHED_DOCUMENTS:
            del self.cache[next(iter(self.cache))]  # the oldest entry goes first
        self.cache[url] = entry
