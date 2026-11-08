"""Client ID Metadata Documents: which URLs may be fetched, how, and what a document must say."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, ClassVar

import pytest

from sreditor import client_metadata
from sreditor.client_metadata import (
    MAX_CACHE_SECONDS,
    MAX_CACHED_DOCUMENTS,
    MAX_DOCUMENT_BYTES,
    ClientDocumentError,
    ClientDocuments,
    FetchedDocument,
    cache_seconds,
    check_url,
    client_from,
    fetch,
    is_client_id_url,
    is_public,
    public_address,
)
from sreditor.oauth import ErrorCode, OAuthError

URL = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def document(**changes: Any) -> bytes:
    fields = {"client_id": URL, "client_name": "Claude", "redirect_uris": [CALLBACK], **changes}
    return json.dumps(fields).encode()


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "::1",
        "fe80::1",
        "fc00::1",
        "0.0.0.0",
        "::ffff:127.0.0.1",
        "::127.0.0.1",
        "64:ff9b::7f00:1",
        "64:ff9b::a00:1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b:1::1",
        "2002:7f00:1::",
        "2001:0:4136:e378:8000:ffff:80ff:fffe",
        "100.64.0.1",
        "224.0.0.1",
    ],
)
def test_is_public_internal_addresses_are_refused(address: str) -> None:
    assert not is_public(address)


@pytest.mark.parametrize(
    "address", ["160.79.104.10", "1.1.1.1", "2606:4700::1111", "64:ff9b::808:808"]
)
def test_is_public_internet_addresses_are_allowed(address: str) -> None:
    assert is_public(address)


def test_public_address_with_one_private_answer_is_refused() -> None:
    with pytest.raises(ClientDocumentError, match="public addresses"):
        public_address("mixed.test", 443, lambda host, port: ["1.1.1.1", "10.0.0.1"])


def test_public_address_without_answers_is_refused() -> None:
    with pytest.raises(ClientDocumentError):
        public_address("empty.test", 443, lambda host, port: [])


def test_public_address_returns_first_public_answer() -> None:
    assert public_address("ok.test", 443, lambda host, port: ["1.1.1.1", "1.0.0.1"]) == "1.1.1.1"


@pytest.mark.parametrize(
    "url", ["http://claude.ai/meta", "https://claude.ai/", "https://claude.ai", "client-123"]
)
def test_is_client_id_url_other_ids_are_not_urls(url: str) -> None:
    assert not is_client_id_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://user:pw@claude.ai/meta", "https://claude.ai/meta#x", "https://claude.ai/a/../meta"],
)
def test_check_url_unsafe_forms_are_refused(url: str) -> None:
    with pytest.raises(ClientDocumentError):
        check_url(url)


def test_check_url_plain_http_is_refused() -> None:
    with pytest.raises(ClientDocumentError, match="https URL"):
        check_url("http://claude.ai/meta")


def test_fetch_private_host_is_refused_before_connecting() -> None:
    with pytest.raises(ClientDocumentError, match="public addresses"):
        fetch("https://internal.test/meta", lambda host, port: ["10.0.0.1"])


def test_resolve_unknown_host_raises() -> None:
    with pytest.raises(ClientDocumentError, match="Cannot resolve"):
        client_metadata.resolve("does-not-exist.invalid", 443)


class FakeResponse:
    def __init__(self, status: int, body: bytes, cache_control: str = "") -> None:
        self.status = status
        self.body = body
        self.cache_control = cache_control
        self.position = 0

    def read(self, limit: int) -> bytes:
        chunk = self.body[self.position : self.position + limit]
        self.position += len(chunk)
        return chunk

    def getheader(self, name: str, default: str) -> str:
        return self.cache_control or default


class FakeConnection:
    """Stands in for the pinned HTTPS connection and records where it would connect."""

    answer = FakeResponse(200, b"{}")
    seen: ClassVar[list[tuple[str, int, str, str]]] = []

    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        self.target = (host, port, address)

    def request(self, method: str, target: str, headers: dict[str, str]) -> None:
        FakeConnection.seen.append((*self.target, target))

    def getresponse(self) -> FakeResponse:
        return FakeConnection.answer

    def close(self) -> None:
        return None


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> type[FakeConnection]:
    monkeypatch.setattr(client_metadata, "PinnedHTTPSConnection", FakeConnection)
    FakeConnection.seen = []
    FakeConnection.answer = FakeResponse(200, document(), "max-age=60")
    return FakeConnection


def public(host: str, port: int) -> list[str]:
    return ["160.79.104.10"]


def test_fetch_connects_to_checked_address_and_keeps_host(connection: type[FakeConnection]) -> None:
    fetch(URL, public)
    assert connection.seen == [
        ("claude.ai", 443, "160.79.104.10", "/oauth/mcp-oauth-client-metadata")
    ]


def test_fetch_returns_body_and_cache_time(connection: type[FakeConnection]) -> None:
    assert fetch(URL, public) == FetchedDocument(body=document(), cache_seconds=60)


def test_fetch_redirect_is_refused(connection: type[FakeConnection]) -> None:
    connection.answer = FakeResponse(302, b"")
    with pytest.raises(ClientDocumentError, match="answered 302"):
        fetch(URL, public)


def test_fetch_oversized_document_is_refused(connection: type[FakeConnection]) -> None:
    connection.answer = FakeResponse(200, b"x" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(ClientDocumentError, match="larger than"):
        fetch(URL, public)


def test_fetch_connection_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failing(FakeConnection):
        def getresponse(self) -> FakeResponse:
            raise OSError("timed out")

    monkeypatch.setattr(client_metadata, "PinnedHTTPSConnection", Failing)
    with pytest.raises(ClientDocumentError, match="timed out"):
        fetch(URL, public)


def test_pinned_connection_connects_to_address_with_host_for_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        client_metadata.socket, "create_connection", lambda target, timeout: calls.append(target)
    )
    connection = client_metadata.PinnedHTTPSConnection("claude.ai", 443, "160.79.104.10", 1.0)
    monkeypatch.setattr(
        connection,
        "_context",
        type(
            "Ctx",
            (),
            {"wrap_socket": lambda self, raw, server_hostname: calls.append(server_hostname)},
        )(),
    )
    connection.connect()
    assert calls == [("160.79.104.10", 443), "claude.ai"]


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("max-age=60", 60), ("no-store", 0), ("", 300), ("max-age=999999", MAX_CACHE_SECONDS)],
)
def test_cache_seconds_follow_cache_control_within_bounds(header: str, seconds: int) -> None:
    assert cache_seconds(header) == seconds


def test_client_from_valid_document_names_client() -> None:
    client = client_from(URL, document())
    assert (client.client_id, client.client_name, client.redirect_uris) == (
        URL,
        "Claude",
        [CALLBACK],
    )


def test_client_from_document_with_other_client_id_is_refused() -> None:
    with pytest.raises(ClientDocumentError, match="does not match"):
        client_from(URL, document(client_id="https://evil.example/meta"))


def test_client_from_document_with_plain_http_redirect_is_refused() -> None:
    with pytest.raises(ClientDocumentError, match="not a valid"):
        client_from(URL, document(redirect_uris=["http://evil.example/cb"]))


def test_client_from_document_supporting_none_is_public_client() -> None:
    body = document(
        token_endpoint_auth_method="private_key_jwt",
        token_endpoint_auth_methods_supported=["none", "private_key_jwt"],
    )
    assert client_from(URL, body).client_id == URL


def test_client_from_document_requiring_private_key_jwt_is_refused() -> None:
    body = document(
        token_endpoint_auth_method="private_key_jwt",
        token_endpoint_auth_methods_supported=["private_key_jwt"],
    )
    with pytest.raises(ClientDocumentError, match="not a valid"):
        client_from(URL, body)


def test_client_from_unreadable_document_is_refused() -> None:
    with pytest.raises(ClientDocumentError, match="not a valid"):
        client_from(URL, b"<html>")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def counting(body: bytes, seconds: int, calls: list[str]) -> Any:
    def fetcher(url: str) -> FetchedDocument:
        calls.append(url)
        return FetchedDocument(body=body, cache_seconds=seconds)

    return fetcher


def test_client_documents_reuses_document_while_fresh() -> None:
    calls: list[str] = []
    documents = ClientDocuments(fetcher=counting(document(), 60, calls), clock=Clock())
    documents.client(URL)
    documents.client(URL)
    assert len(calls) == 1


def test_client_documents_fetches_again_after_expiry() -> None:
    calls: list[str] = []
    clock = Clock()
    documents = ClientDocuments(fetcher=counting(document(), 60, calls), clock=clock)
    documents.client(URL)
    clock.now = 61
    documents.client(URL)
    assert len(calls) == 2


def test_client_documents_no_store_is_not_cached() -> None:
    calls: list[str] = []
    documents = ClientDocuments(fetcher=counting(document(), 0, calls), clock=Clock())
    documents.client(URL)
    documents.client(URL)
    assert len(calls) == 2


def test_client_documents_bad_document_is_invalid_client() -> None:
    documents = ClientDocuments(fetcher=counting(b"{}", 60, []), clock=Clock())
    with pytest.raises(OAuthError) as raised:
        documents.client(URL)
    assert raised.value.code is ErrorCode.INVALID_CLIENT


def many_documents(count: int, clock: Clock) -> ClientDocuments:
    """A cache filled with `count` distinct valid documents, each fresh for 60 seconds."""

    def fetcher(url: str) -> FetchedDocument:
        return FetchedDocument(body=document(client_id=url), cache_seconds=60)

    documents = ClientDocuments(fetcher=fetcher, clock=clock)
    for index in range(count):
        documents.client(f"{URL}/{index}")
    return documents


def test_client_documents_cache_never_exceeds_its_cap() -> None:
    documents = many_documents(MAX_CACHED_DOCUMENTS + 10, Clock())
    assert len(documents.cache) == MAX_CACHED_DOCUMENTS


def test_client_documents_full_cache_drops_oldest_first() -> None:
    documents = many_documents(MAX_CACHED_DOCUMENTS + 1, Clock())
    assert f"{URL}/0" not in documents.cache and f"{URL}/1" in documents.cache


def test_client_documents_full_cache_drops_expired_entries() -> None:
    clock = Clock()
    documents = many_documents(MAX_CACHED_DOCUMENTS, clock)
    clock.now = 61
    documents.client(f"{URL}/new")
    assert list(documents.cache) == [f"{URL}/new"]


def slow_resolver(host: str, port: int) -> list[str]:
    time.sleep(1.0)
    return ["160.79.104.10"]


def test_fetch_slow_name_lookup_stops_at_the_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(ClientDocumentError, match="did not answer within"):
        fetch(URL, slow_resolver, deadline_seconds=0.1)
    assert time.monotonic() - started < 0.9


def test_download_after_its_deadline_is_abandoned(connection: type[FakeConnection]) -> None:
    with pytest.raises(ClientDocumentError, match="not fetched in time"):
        client_metadata.download(URL, public, time.monotonic() - 1)
    assert connection.seen == []


class TrickleResponse(FakeResponse):
    """Sends one byte per read, slowly, like a server trying to hold a connection open."""

    def read(self, limit: int) -> bytes:
        time.sleep(0.05)
        return b"x"


def test_download_trickling_body_stops_at_the_deadline(connection: type[FakeConnection]) -> None:
    connection.answer = TrickleResponse(200, b"")
    with pytest.raises(ClientDocumentError, match="too slow"):
        client_metadata.download(URL, public, time.monotonic() + 0.2)


class CountingLock:
    """A lock that counts how often it was taken."""

    def __init__(self) -> None:
        self.taken = 0
        self.inner = threading.Lock()

    def __enter__(self) -> None:
        self.taken += 1
        self.inner.acquire()

    def __exit__(self, *exc: object) -> None:
        self.inner.release()


def test_client_documents_cache_is_read_and_written_under_its_lock() -> None:
    lock = CountingLock()
    documents = ClientDocuments(
        fetcher=counting(document(), 60, []),
        clock=Clock(),
        lock=lock,  # type: ignore[arg-type]
    )
    documents.client(URL)
    assert lock.taken == 2


def test_client_documents_concurrent_callers_keep_the_cache_within_its_cap() -> None:
    clock = Clock()

    def fetcher(url: str) -> FetchedDocument:
        return FetchedDocument(body=document(client_id=url), cache_seconds=60)

    documents = ClientDocuments(fetcher=fetcher, clock=clock)
    failures: list[Exception] = []

    def hammer(worker: int) -> None:
        try:
            for index in range(MAX_CACHED_DOCUMENTS):
                documents.client(f"{URL}/{worker}/{index}")
        except Exception as raised:  # noqa: BLE001 - the test reports whatever a thread hit
            failures.append(raised)

    threads = [threading.Thread(target=hammer, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == [] and len(documents.cache) <= MAX_CACHED_DOCUMENTS
