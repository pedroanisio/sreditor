from __future__ import annotations

from pathlib import Path

import pytest
import uvicorn

from sreditor import cli
from sreditor.rules_file import (
    RULES_CSV,
    RulesFileError,
    load_rules,
    parse_rules_csv,
    render_rules_csv,
)
from sreditor.store import Statement, StmtClient, StoreSettings

OWNER = {"type": "user", "id": "pedro"}
SETTINGS = StoreSettings(db="unused.sqlite", domain="test")
CHANNEL_SCOPE = "/channel"


def statement(statement_id: str, text: str = "A rule.", **fields: object) -> Statement:
    data = {
        "id": statement_id,
        "revision": 1,
        "scope": "/",
        "statement": text,
        "owner": OWNER,
        "category": "code",
        "priority": 1,
        **fields,
    }
    return Statement.model_validate(data)


FULL = statement(
    "gate.write",
    'Line one, "quoted".\nLine two.',
    conditionals=["ctx.branch == 'main'"],
    rule={
        "effect": "BLOCK",
        "trigger": {"kind": "EVENT", "phase": "BEFORE", "operation": "code.write"},
    },
    evidence=[{"ref": "docs/review.md"}],
    ttl_seconds=3600,
)


def round_trip(statements: list[Statement]) -> list[Statement]:
    return parse_rules_csv(render_rules_csv(statements, SETTINGS))


def test_rules_csv_round_trip_keeps_every_field() -> None:
    [back] = round_trip([FULL])
    assert back.document() == FULL.document()


def test_rules_csv_round_trip_keeps_override_key_and_requires() -> None:
    plain = statement("a.rule", override_key="tone", requires=["b.rule"])
    assert [item.document() for item in round_trip([plain])] == [plain.document()]


def test_rules_csv_rows_are_sorted_by_id() -> None:
    back = round_trip([statement("b.rule"), statement("a.rule")])
    assert [item.id for item in back] == ["a.rule", "b.rule"]


def test_rules_csv_leaves_out_other_scopes() -> None:
    assert round_trip([statement("a.rule"), statement("c.rule", scope=CHANNEL_SCOPE)]) == [
        statement("a.rule", revision=0)
    ]


def test_rules_csv_with_other_columns_is_refused() -> None:
    with pytest.raises(RulesFileError, match="columns"):
        parse_rules_csv("id,statement\na,b\n")


def test_rules_csv_with_bad_cell_names_its_line() -> None:
    text = render_rules_csv([statement("a.rule")], SETTINGS).replace(",1,user", ",high,user")
    with pytest.raises(RulesFileError, match="line 2"):
        parse_rules_csv(text)


@pytest.fixture
def client(store: StoreSettings) -> StmtClient:
    return StmtClient(store)


def test_load_rules_into_empty_store_creates_each(client: StmtClient) -> None:
    assert load_rules(client, [statement("a.rule"), FULL]).created == ["a.rule", "gate.write"]


def test_load_rules_twice_changes_nothing(client: StmtClient) -> None:
    load_rules(client, [statement("a.rule"), FULL])
    assert (
        load_rules(client, [statement("a.rule"), FULL]).summary()
        == "rules: 0 created, 0 revised, 0 disabled"
    )


def test_load_rules_changed_text_revises_statement(client: StmtClient) -> None:
    load_rules(client, [statement("a.rule")])
    assert load_rules(client, [statement("a.rule", "New text.")]).revised == ["a.rule"]
    assert [head.statement for head in client.statements()] == ["New text."]


def test_load_rules_statement_missing_from_csv_is_disabled(client: StmtClient) -> None:
    load_rules(client, [statement("a.rule"), statement("b.rule")])
    assert load_rules(client, [statement("a.rule")]).disabled == ["b.rule"]
    assert [head.id for head in client.statements()] == ["a.rule"]


def test_load_rules_disabled_statement_back_in_csv_is_enabled(client: StmtClient) -> None:
    load_rules(client, [statement("a.rule")])
    load_rules(client, [])
    assert load_rules(client, [statement("a.rule")]).revised == ["a.rule"]


def test_load_rules_leaves_channel_scope_alone(client: StmtClient) -> None:
    client.add_scope(CHANNEL_SCOPE)
    client.write("channel.voice", statement("channel.voice").document(), CHANNEL_SCOPE)
    load_rules(client, [statement("a.rule")])
    assert client.get("channel.voice", CHANNEL_SCOPE) is not None


def write_csv(root: Path, statements: list[Statement]) -> None:
    path = root / RULES_CSV
    path.parent.mkdir(parents=True)
    path.write_text(render_rules_csv(statements, SETTINGS), encoding="utf-8")


def store_flags(store: StoreSettings) -> list[str]:
    return ["--stmt", store.stmt, "--db", store.db, "--domain", store.domain]


def test_cli_rules_load_prints_what_changed(
    tmp_path: Path, store: StoreSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    write_csv(tmp_path, [statement("a.rule")])
    args = ["rules", "load", "--root", str(tmp_path), *store_flags(store)]
    assert cli.main(args, environ={}) == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "rules: 1 created, 0 revised, 0 disabled"


def test_cli_rules_load_without_csv_exits_with_error(
    tmp_path: Path, store: StoreSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["rules", "load", "--root", str(tmp_path), *store_flags(store)]
    assert cli.main(args, environ={}) == cli.EXIT_ERROR
    assert "rules.csv" in capsys.readouterr().err


def test_cli_serve_with_load_rules_loads_csv_before_serving(
    tmp_path: Path, store: StoreSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    write_csv(tmp_path, [statement("a.rule")])
    environ = {"STMT_BIN": store.stmt, "STMT_DB": store.db, "STMT_DOMAIN": store.domain}
    environ["SREDITOR_CHANNELS_DB"] = str(tmp_path / "channels.sqlite")
    cli.main(["serve", "--root", str(tmp_path), "--load-rules"], environ=environ)
    assert [head.id for head in StmtClient(store).statements()] == ["a.rule"]
