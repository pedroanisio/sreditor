"""Request metrics: route templates as labels, status codes, durations and the metrics port."""

from __future__ import annotations

from pathlib import Path

import prometheus_client
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sreditor import metrics
from sreditor.settings import AppSettings

REQUESTS_TOTAL = "sreditor_http_requests_total"
DURATION_COUNT = "sreditor_http_request_duration_seconds_count"
METRICS_PORT = 9100
# The least a store needs, so these tests load settings without a pyproject.toml.
STORE_ENVIRONMENT = {"STMT_DB": "rules.sqlite", "STMT_DOMAIN": "demo"}


class Broken(RuntimeError):
    """Raised by the test endpoint that fails."""


def build_app() -> FastAPI:
    """A small app with one templated route and one that fails."""
    app = FastAPI()
    app.add_middleware(metrics.MetricsMiddleware)

    @app.get("/items/{item_id}")
    def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @app.get("/broken")
    def broken() -> None:
        raise Broken("on purpose")

    return app


def sample(name: str, labels: dict[str, str]) -> float:
    """The current value of one series, zero before it first appears."""
    return prometheus_client.REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(), raise_server_exceptions=False)


def test_metrics_matched_route_counts_by_template(client: TestClient) -> None:
    labels = {"method": "GET", "route": "/items/{item_id}", "status": "200"}
    before = sample(REQUESTS_TOTAL, labels)
    client.get("/items/7")
    client.get("/items/8")
    assert sample(REQUESTS_TOTAL, labels) == before + 2


def test_metrics_unknown_path_counts_as_unmatched(client: TestClient) -> None:
    labels = {"method": "GET", "route": metrics.UNMATCHED_ROUTE, "status": "404"}
    before = sample(REQUESTS_TOTAL, labels)
    client.get("/nowhere/at/all")
    assert sample(REQUESTS_TOTAL, labels) == before + 1


def test_metrics_failing_endpoint_counts_as_server_error(client: TestClient) -> None:
    labels = {"method": "GET", "route": "/broken", "status": "500"}
    before = sample(REQUESTS_TOTAL, labels)
    client.get("/broken")
    assert sample(REQUESTS_TOTAL, labels) == before + 1


def test_metrics_request_records_its_duration(client: TestClient) -> None:
    labels = {"method": "GET", "route": "/items/{item_id}"}
    before = sample(DURATION_COUNT, labels)
    client.get("/items/1")
    assert sample(DURATION_COUNT, labels) == before + 1


def test_metrics_lifespan_scope_passes_through() -> None:
    with TestClient(build_app()) as client:
        assert client.get("/items/2").status_code == 200


def test_metrics_serve_listens_on_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        prometheus_client, "start_http_server", lambda port, addr: calls.append((port, addr))
    )
    metrics.serve("0.0.0.0", METRICS_PORT)
    assert calls == [(METRICS_PORT, "0.0.0.0")]


def test_app_settings_metrics_port_defaults_to_none(tmp_path: Path) -> None:
    assert AppSettings.load(tmp_path, STORE_ENVIRONMENT).metrics_port is None


def test_app_settings_metrics_port_from_environment(tmp_path: Path) -> None:
    loaded = AppSettings.load(
        tmp_path, {**STORE_ENVIRONMENT, "SREDITOR_METRICS_PORT": str(METRICS_PORT)}
    )
    assert loaded.metrics_port == METRICS_PORT


def test_app_settings_metrics_port_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.sreditor]\nmetrics_port = '9200'\n")
    assert AppSettings.load(tmp_path, STORE_ENVIRONMENT).metrics_port == 9200
