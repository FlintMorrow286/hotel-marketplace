import asyncio
from types import SimpleNamespace

from fastapi import FastAPI

import app.main as main
from app.config.settings import settings


def _idle_loop(name: str, started: list[str], stopped: list[str]):
    async def loop(*args) -> None:
        started.append(name)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(name)

    return loop


def _patch_lifespan(monkeypatch, run_background_jobs: bool):
    from app.jobs import reaper
    from app.modules.outbox import retention, worker
    from app.modules.sync import poller
    from app.utils import redis, secrets

    started: list[str] = []
    stopped: list[str] = []
    closed: list[str] = []
    pool = SimpleNamespace(get_size=lambda: 1)

    class FakeRedis:
        async def info(self, section: str) -> dict[str, str]:
            return {"redis_version": "test"}

    async def init_pool():
        return pool

    async def run_migrations():
        return []

    async def init_redis():
        return FakeRedis()

    async def close_redis():
        closed.append("redis")

    async def close_pool():
        closed.append("pool")

    async def outbox_loop(group: int) -> None:
        await _idle_loop(f"outbox-{group}", started, stopped)()

    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main, "check_config", lambda _: None)
    monkeypatch.setattr(main, "init_pool", init_pool)
    monkeypatch.setattr(main, "run_migrations", run_migrations)
    monkeypatch.setattr(main, "close_pool", close_pool)
    monkeypatch.setattr(settings, "run_background_jobs", run_background_jobs)
    monkeypatch.setattr(settings, "sentry_dsn", "")
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(redis, "init_redis", init_redis)
    monkeypatch.setattr(redis, "close_redis", close_redis)
    monkeypatch.setattr(secrets, "seal_key_is_configured", lambda: True)
    monkeypatch.setattr(
        reaper, "reaper_loop", _idle_loop("reaper", started, stopped)
    )
    monkeypatch.setattr(
        poller, "import_loop", _idle_loop("import", started, stopped)
    )
    monkeypatch.setattr(
        retention, "retention_loop", _idle_loop("retention", started, stopped)
    )
    monkeypatch.setattr(worker, "outbox_loop", outbox_loop)
    monkeypatch.setattr(worker, "shard_groups", lambda: (0, 1))
    return started, stopped, closed


def test_background_jobs_are_not_started_when_disabled(monkeypatch):
    started, stopped, closed = _patch_lifespan(monkeypatch, False)

    async def exercise_lifespan():
        async with main.lifespan(FastAPI()):
            await asyncio.sleep(0)
            assert started == []

    asyncio.run(exercise_lifespan())
    assert stopped == []
    assert closed == ["redis", "pool"]


def test_background_jobs_start_when_enabled(monkeypatch):
    started, stopped, closed = _patch_lifespan(monkeypatch, True)
    expected = {"reaper", "import", "outbox-0", "outbox-1", "retention"}

    async def exercise_lifespan():
        async with main.lifespan(FastAPI()):
            await asyncio.sleep(0)
            assert set(started) == expected

    asyncio.run(exercise_lifespan())
    assert set(stopped) == expected
    assert closed == ["redis", "pool"]
