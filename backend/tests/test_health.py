"""Health endpoint behaviour, with dependencies faked.

These run without Docker: they verify the endpoint's contract and its failure
handling. Verifying the real containers is a manual Phase 0 step (see README).
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_health_ok_when_dependencies_are_up(client: TestClient) -> None:
    with (
        patch("app.api.health._check_db", return_value="ok"),
        patch("app.api.health._check_redis", return_value="ok"),
    ):
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "redis": "ok"}


def test_health_reports_503_when_db_is_down(client: TestClient) -> None:
    """A down dependency must surface as 503, so a load balancer removes the node."""
    with (
        patch("app.api.health._check_db", return_value="error"),
        patch("app.api.health._check_redis", return_value="ok"),
    ):
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "error", "redis": "ok"}


def test_health_does_not_leak_connection_details(client: TestClient) -> None:
    """Security invariant #5: an error must not expose credentials or hosts."""
    broken = MagicMock()
    broken.connect.side_effect = RuntimeError("password=hunter2 host=internal.db")

    with (
        patch("app.api.health.engine", broken),
        patch("app.api.health._check_redis", return_value="ok"),
    ):
        response = client.get("/health")

    assert response.status_code == 503
    assert "hunter2" not in response.text
    assert "internal.db" not in response.text
