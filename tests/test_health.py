"""Liveness and readiness: a container healthcheck must be able to tell sick from well."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.main import create_app


class _DeadPool:
    async def execute(self, *args, **kwargs):
        raise RuntimeError("password authentication failed for user hotel_user at 10.0.0.5")


class _DeadRedis:
    async def ping(self):
        raise RuntimeError("Error 111 connecting to redis.internal:6379")


@pytest.mark.asyncio
async def test_ready_when_the_database_and_redis_answer(committed_conn) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"db": "ok", "redis": "ok"}}


@pytest.mark.asyncio
async def test_not_ready_when_the_database_is_down(committed_conn, monkeypatch) -> None:
    with TestClient(create_app()) as client:
        monkeypatch.setattr("app.db.pool.get_pool", lambda: _DeadPool())
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "checks": {"db": "error", "redis": "ok"}}


@pytest.mark.asyncio
async def test_not_ready_when_redis_is_down(committed_conn, monkeypatch) -> None:
    with TestClient(create_app()) as client:
        monkeypatch.setattr("app.utils.redis.get_redis", lambda: _DeadRedis())
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"] == {"db": "ok", "redis": "error"}


@pytest.mark.asyncio
async def test_the_cause_goes_to_the_log_not_to_the_caller(committed_conn, monkeypatch) -> None:
    logged: list[dict] = []
    monkeypatch.setattr("app.main.log.warning", lambda event, **kw: logged.append({event: kw}))

    with TestClient(create_app()) as client:
        monkeypatch.setattr("app.db.pool.get_pool", lambda: _DeadPool())
        monkeypatch.setattr("app.utils.redis.get_redis", lambda: _DeadRedis())
        response = client.get("/readyz")

    body = response.text
    assert "password" not in body and "10.0.0.5" not in body and "redis.internal" not in body
    causes = {
        item["readyz-check-failed"]["check"]: item["readyz-check-failed"]["error"]
        for item in logged
        if "readyz-check-failed" in item
    }
    assert "password authentication failed" in causes["db"]
    assert "redis.internal" in causes["redis"]


@pytest.mark.asyncio
async def test_liveness_never_touches_the_database(committed_conn, monkeypatch) -> None:
    with TestClient(create_app()) as client:
        monkeypatch.setattr("app.db.pool.get_pool", lambda: _DeadPool())
        response = client.get("/healthz")

    assert response.status_code == 200 and response.json() == {"status": "ok"}
