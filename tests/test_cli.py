from __future__ import annotations

import getpass
from pathlib import Path

import pytest
import uvicorn

from sreditor import cli, metrics
from sreditor import settings as settings_module
from sreditor.database import Database
from sreditor.settings import AppSettings
from sreditor.store import Statement, StmtClient, StoreError, StoreSettings
from sreditor.users import UserStatus, UserStore
from tests.conftest import STATEMENTS

ADMIN_PASSWORD_VARIABLE = "SREDITOR_ADMIN_PASSWORD"
ADMIN_EMAIL = "root@example.com"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sreditor]\ndb = "rules.sqlite"\ndomain = "demo"\n', encoding="utf-8"
    )
    statements = [Statement.model_validate(item) for item in STATEMENTS]
    monkeypatch.setattr(StmtClient, "statements", lambda self: statements)
    return tmp_path


def run(project: Path, *args: str, environ: dict[str, str] | None = None) -> int:
    return cli.main(["render", "--root", str(project), *args], environ=environ or {})


def test_render_first_run_writes_documents(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(project) == cli.EXIT_OK
    assert "wrote CLAUDE.md" in capsys.readouterr().out
    assert (project / "README.md").read_text(encoding="utf-8").startswith("<!-- Generated")


def test_render_check_after_write_exits_ok(project: Path) -> None:
    run(project)
    assert run(project, "--check") == cli.EXIT_OK


def test_render_rerun_without_changes_prints_nothing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(project)
    capsys.readouterr()

    assert run(project) == cli.EXIT_OK
    assert capsys.readouterr().out == ""


@pytest.fixture
def stale_project(project: Path) -> Path:
    (project / "CLAUDE.md").write_text("hand edited\n", encoding="utf-8")
    return project


def test_render_check_stale_document_exits_stale_and_names_it(
    stale_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(stale_project, "--check", "--only", "claude") == cli.EXIT_STALE
    assert "out of date: CLAUDE.md" in capsys.readouterr().err


def test_render_check_stale_document_is_not_rewritten(stale_project: Path) -> None:
    run(stale_project, "--check", "--only", "claude")
    assert (stale_project / "CLAUDE.md").read_text(encoding="utf-8") == "hand edited\n"
    assert not (stale_project / "README.md").exists()


def test_render_store_error_exits_with_error(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(self: StmtClient) -> list[Statement]:
        raise StoreError("store is down")

    monkeypatch.setattr(StmtClient, "statements", fail)
    assert run(project) == cli.EXIT_ERROR
    assert "store is down" in capsys.readouterr().err


def test_render_missing_domain_exits_with_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path) == cli.EXIT_ERROR
    assert "domain" in capsys.readouterr().err


@pytest.fixture
def resolved_settings(tmp_path: Path) -> StoreSettings:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sreditor]\ndb = "rules.sqlite"\ndomain = "file"\nscope = "/repo"\n', encoding="utf-8"
    )
    environ = {"STMT_DOMAIN": "env", "STMT_BIN": "/opt/stmt", "STMT_DB": ""}
    flags = {"stmt": None, "db": None, "domain": "flag", "scope": None}
    return settings_module.resolve_settings(tmp_path, flags, environ)


def test_resolve_settings_flag_beats_environment_and_file_for_domain(
    resolved_settings: StoreSettings,
) -> None:
    assert resolved_settings.domain == "flag"


def test_resolve_settings_environment_supplies_stmt_binary(
    resolved_settings: StoreSettings,
) -> None:
    assert resolved_settings.stmt == "/opt/stmt"


def test_resolve_settings_file_supplies_scope(
    resolved_settings: StoreSettings,
) -> None:
    assert resolved_settings.scope == "/repo"


def test_resolve_settings_empty_environment_db_uses_file_path_under_root(
    resolved_settings: StoreSettings, tmp_path: Path
) -> None:
    assert resolved_settings.db == str(tmp_path / "rules.sqlite")


def test_project_settings_without_pyproject_returns_empty(tmp_path: Path) -> None:
    assert settings_module.project_settings(tmp_path) == {}


@pytest.fixture
def file_configured_root(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sreditor]\ndb = "store/rules.sqlite"\ndomain = "demo"\nchannels_db = "data/c.sqlite"\n',
        encoding="utf-8",
    )
    return tmp_path


def test_app_settings_load_file_channels_db_resolves_under_root(file_configured_root: Path) -> None:
    loaded = AppSettings.load(file_configured_root, {})
    assert loaded.channels_db == file_configured_root / "data" / "c.sqlite"


def test_app_settings_load_file_store_db_resolves_under_root(file_configured_root: Path) -> None:
    loaded = AppSettings.load(file_configured_root, {})
    assert loaded.store.db == str(file_configured_root / "store" / "rules.sqlite")


def test_app_settings_load_without_owner_defaults_to_sreditor_service(
    file_configured_root: Path,
) -> None:
    loaded = AppSettings.load(file_configured_root, {})
    assert (loaded.owner.type, loaded.owner.id) == ("service", "sreditor")


def test_app_settings_load_environment_overrides_channels_db(file_configured_root: Path) -> None:
    loaded = AppSettings.load(file_configured_root, {"SREDITOR_CHANNELS_DB": "/srv/c.sqlite"})
    assert loaded.channels_db == Path("/srv/c.sqlite")


def test_app_settings_load_environment_overrides_owner(file_configured_root: Path) -> None:
    environ = {"SREDITOR_OWNER_TYPE": "user", "SREDITOR_OWNER_ID": "p"}
    loaded = AppSettings.load(file_configured_root, environ)
    assert (loaded.owner.type, loaded.owner.id) == ("user", "p")


def test_app_settings_load_mail_log_tokens_defaults_to_false(file_configured_root: Path) -> None:
    assert AppSettings.load(file_configured_root, {}).has_tokens_in_mail_log is False


def test_app_settings_load_mail_log_tokens_true_from_environment(
    file_configured_root: Path,
) -> None:
    loaded = AppSettings.load(file_configured_root, {"SREDITOR_MAIL_LOG_TOKENS": "TRUE"})
    assert loaded.has_tokens_in_mail_log is True


@pytest.fixture
def uvicorn_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Keyword arguments of each uvicorn.run call, instead of starting a server."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    return calls


def test_serve_passes_host_and_port_to_uvicorn(
    project: Path, uvicorn_calls: list[dict[str, object]]
) -> None:
    args = ["serve", "--root", str(project), "--host", "0.0.0.0", "--port", "9000"]
    assert cli.main(args, environ={}) == cli.EXIT_OK
    assert [(call["host"], call["port"]) for call in uvicorn_calls] == [("0.0.0.0", 9000)]


def test_serve_installs_redact_filter_on_every_log_handler(
    project: Path, uvicorn_calls: list[dict[str, object]]
) -> None:
    cli.main(["serve", "--root", str(project)], environ={})
    log_config = uvicorn_calls[0]["log_config"]
    assert isinstance(log_config, dict)
    assert all("redact" in handler["filters"] for handler in log_config["handlers"].values())


def test_serve_trusts_forwarded_headers_only_from_localhost_by_default(
    project: Path, uvicorn_calls: list[dict[str, object]]
) -> None:
    cli.main(["serve", "--root", str(project)], environ={})
    assert uvicorn_calls[0]["proxy_headers"] is True
    assert uvicorn_calls[0]["forwarded_allow_ips"] == "127.0.0.1"


def test_serve_with_forwarded_allow_ips_trusts_configured_proxies(
    project: Path, uvicorn_calls: list[dict[str, object]]
) -> None:
    cli.main(["serve", "--root", str(project)], environ={"SREDITOR_FORWARDED_ALLOW_IPS": "*"})
    assert uvicorn_calls[0]["forwarded_allow_ips"] == "*"


@pytest.fixture
def admin_args(project: Path) -> list[str]:
    return ["create-admin", "--root", str(project), "--email", "Root@Example.com", "--name", "Root"]


def test_create_admin_prints_normalized_email(
    admin_args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    environ = {ADMIN_PASSWORD_VARIABLE: "a long admin passphrase"}
    assert cli.main(admin_args, environ=environ) == cli.EXIT_OK
    assert f"created admin {ADMIN_EMAIL}" in capsys.readouterr().out


def test_create_admin_stores_an_active_admin_user(project: Path, admin_args: list[str]) -> None:
    cli.main(admin_args, environ={ADMIN_PASSWORD_VARIABLE: "a long admin passphrase"})

    users = UserStore(Database(AppSettings.load(project, {}).channels_db))
    found = users.credentials(ADMIN_EMAIL)
    assert found is not None
    assert (found[0].is_admin, found[0].status) == (True, UserStatus.ACTIVE)


def test_create_admin_existing_email_exits_with_error(
    admin_args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    environ = {ADMIN_PASSWORD_VARIABLE: "a long admin passphrase"}
    cli.main(admin_args, environ=environ)
    capsys.readouterr()

    assert cli.main(admin_args, environ=environ) == cli.EXIT_ERROR
    assert "already has an account" in capsys.readouterr().err


def test_create_admin_short_password_exits_with_error(admin_args: list[str]) -> None:
    assert cli.main(admin_args, environ={ADMIN_PASSWORD_VARIABLE: "short"}) == cli.EXIT_ERROR


def test_create_admin_prompts_for_password(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "a prompted passphrase")
    args = ["create-admin", "--root", str(project), "--email", "p@example.com", "--name", "P"]
    assert cli.main(args, environ={}) == cli.EXIT_OK


def test_serve_mail_log_tokens_reveals_mail_logger(
    project: Path, uvicorn_calls: list[dict[str, object]]
) -> None:
    args = ["serve", "--root", str(project)]
    assert cli.main(args, environ={"SREDITOR_MAIL_LOG_TOKENS": "true"}) == cli.EXIT_OK
    log_config = uvicorn_calls[0]["log_config"]
    assert isinstance(log_config, dict)
    assert log_config["filters"]["redact"]["reveal"] == ["sreditor.mail"]


def test_app_settings_load_reads_agent_request_limit(file_configured_root: Path) -> None:
    loaded = AppSettings.load(file_configured_root, {"SREDITOR_AGENT_REQUESTS_PER_MINUTE": "7"})
    assert loaded.agent_requests_per_minute == 7


METRICS_PORT = 9100


@pytest.fixture
def metrics_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Each metrics.serve call, with uvicorn stubbed so no server starts."""
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(metrics, "serve", lambda host, port: calls.append((host, port)))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    return calls


def test_serve_with_metrics_port_starts_metrics_listener(
    project: Path, metrics_calls: list[tuple[str, int]]
) -> None:
    environ = {"SREDITOR_METRICS_PORT": str(METRICS_PORT)}
    cli.main(["serve", "--root", str(project), "--host", "0.0.0.0"], environ=environ)
    assert metrics_calls == [("0.0.0.0", METRICS_PORT)]


def test_serve_without_metrics_port_starts_no_listener(
    project: Path, metrics_calls: list[tuple[str, int]]
) -> None:
    cli.main(["serve", "--root", str(project)], environ={})
    assert metrics_calls == []
