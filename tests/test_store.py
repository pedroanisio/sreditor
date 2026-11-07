from __future__ import annotations

import json
import sys

import pytest

from sreditor.store import StmtClient, StoreError, StoreSettings, run_process
from tests.conftest import STATEMENTS, FakeStmt, ok_output


def test_statements_parses_listed_statements_and_their_rules(settings: StoreSettings) -> None:
    statements = StmtClient(settings, runner=FakeStmt(ok_output(STATEMENTS))).statements()

    assert [statement.id for statement in statements] == [item["id"] for item in STATEMENTS]
    review = next(statement for statement in statements if statement.id == "code.review")
    assert review.rule is not None and review.rule.effect == "BLOCK"


def test_statements_call_passes_list_command_and_store_boundary(settings: StoreSettings) -> None:
    fake = FakeStmt(ok_output(STATEMENTS))
    StmtClient(settings, runner=fake).statements()

    args, env = fake.calls[0]
    assert args == ["stmt", "list"]
    assert (env["STMT_DB"], env["STMT_DOMAIN"], env["STMT_SCOPE"]) == (
        "/tmp/rules.sqlite",
        "demo",
        "/",
    )


def test_statements_error_envelope_raises_store_error_with_code_and_message(
    settings: StoreSettings,
) -> None:
    body = {"protocol": 1, "ok": False, "error": {"code": "NOT_FOUND", "message": "gone"}}
    fake = FakeStmt(json.dumps(body), returncode=6)
    with pytest.raises(StoreError, match="NOT_FOUND: gone"):
        StmtClient(settings, runner=fake).statements()


def test_statements_error_envelope_without_detail_raises_unknown_code(
    settings: StoreSettings,
) -> None:
    fake = FakeStmt(json.dumps({"protocol": 1, "ok": False}), returncode=1)
    with pytest.raises(StoreError, match="UNKNOWN"):
        StmtClient(settings, runner=fake).statements()


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [("not json", "boom", "boom"), ("", "", "exit 1"), ("garbage", "", "garbage")],
)
def test_statements_unparseable_output_raises_store_error_with_diagnostic(
    settings: StoreSettings, stdout: str, stderr: str, expected: str
) -> None:
    fake = FakeStmt(stdout, returncode=1, stderr=stderr)
    with pytest.raises(StoreError, match=expected):
        StmtClient(settings, runner=fake).statements()


def test_statements_unsupported_protocol_raises_store_error(settings: StoreSettings) -> None:
    fake = FakeStmt(json.dumps({"protocol": 2, "ok": True, "result": []}))
    with pytest.raises(StoreError, match="protocol 2"):
        StmtClient(settings, runner=fake).statements()


@pytest.mark.parametrize("result", [None, [{"id": "x"}]])
def test_statements_non_list_result_raises_store_error(
    settings: StoreSettings, result: object
) -> None:
    with pytest.raises(StoreError, match="Unexpected statement list"):
        StmtClient(settings, runner=FakeStmt(ok_output(result))).statements()


def test_statements_missing_executable_raises_cannot_run() -> None:
    settings = StoreSettings(stmt="/nonexistent/stmt", db="x", domain="d")
    with pytest.raises(StoreError, match="Cannot run"):
        StmtClient(settings).statements()


def test_run_process_command_returns_exit_code_and_stdout() -> None:
    done = run_process([sys.executable, "-c", "print('hi')"], {})
    assert (done.returncode, done.stdout) == (0, "hi\n")


def test_resolve_unexpected_result_raises_store_error(settings: StoreSettings) -> None:
    with pytest.raises(StoreError, match="Unexpected resolve result"):
        StmtClient(settings, runner=FakeStmt(ok_output({"nothing": []}))).resolve("/")


def test_get_error_other_than_not_found_reraises_with_code(settings: StoreSettings) -> None:
    body = {"protocol": 1, "ok": False, "error": {"code": "BUSY", "message": "locked"}}
    with pytest.raises(StoreError, match="BUSY") as caught:
        StmtClient(settings, runner=FakeStmt(json.dumps(body), returncode=5)).get("x", "/")
    assert caught.value.code == "BUSY"


SCOPE = "/a"
DOCUMENT = {"statement": "Hi.", "owner": {"type": "user", "id": "t"}, "category": "c"}


@pytest.fixture
def scoped_client(store: StoreSettings) -> StmtClient:
    client = StmtClient(store)
    client.add_scope(SCOPE)
    return client


@pytest.fixture
def written_client(scoped_client: StmtClient) -> StmtClient:
    scoped_client.write("a.hi", DOCUMENT, SCOPE)
    return scoped_client


def test_write_new_statement_returns_first_revision(scoped_client: StmtClient) -> None:
    assert scoped_client.write("a.hi", DOCUMENT, SCOPE) == 1


def test_get_written_statement_returns_head_in_its_scope(written_client: StmtClient) -> None:
    head = written_client.get("a.hi", SCOPE)
    assert head is not None and head.scope == SCOPE


def test_write_with_expected_revision_returns_next_revision(written_client: StmtClient) -> None:
    head = written_client.get("a.hi", SCOPE)
    assert head is not None
    updated = {**head.document(), "statement": "Hello."}
    assert written_client.write("a.hi", updated, SCOPE, head.revision) == 2


def test_resolve_after_update_returns_latest_statement(written_client: StmtClient) -> None:
    head = written_client.get("a.hi", SCOPE)
    assert head is not None
    written_client.write("a.hi", {**head.document(), "statement": "Hello."}, SCOPE, head.revision)
    assert [statement.statement for statement in written_client.resolve(SCOPE)] == ["Hello."]


def test_get_in_scope_without_the_statement_returns_none(written_client: StmtClient) -> None:
    assert written_client.get("a.hi", "/") is None
