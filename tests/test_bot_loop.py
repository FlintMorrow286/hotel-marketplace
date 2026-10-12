"""The Telegram bot inside the jobs process: what a bad token does, and how the process stops.

TELEGRAM_BOT_TOKEN reaches the containers since docker-compose.yml passes every key of .env.example. Measured on the
Compose stack: a malformed token made the bot task die with nothing in the log, and the failure surfaced only when the
process stopped, as "Application shutdown failed", with the connection pools never closed; a token that Telegram does
not know logged one error, but the container was then killed after its 10 s grace period (exit code 137), because
aiogram's start_polling takes over SIGTERM and SIGINT from uvicorn and, once polling is over, leaves handlers that do
nothing.
"""

from __future__ import annotations

import asyncio
import signal

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramUnauthorizedError
from fastapi.testclient import TestClient

from app import main as app_main
from app.config.settings import settings
from app.modules import bot as bot_module

WELL_FORMED_TOKEN = "123456789:" + "A" * 35


class _Recorder:
    """Stands in for the module's structlog logger and keeps what was logged."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        return lambda event, **fields: self.events.append((level, event, fields))


@pytest.mark.asyncio
async def test_a_malformed_token_is_logged_at_once(monkeypatch) -> None:
    log = _Recorder()
    monkeypatch.setattr(bot_module, "log", log)

    await bot_module.bot_loop("notatoken")

    failed = [fields for level, event, fields in log.events if event == "bot-polling-failed"]
    assert len(failed) == 1 and "invalid" in failed[0]["error"].lower(), log.events
    assert not [e for _, e, _ in log.events if e == "bot-started"]


@pytest.mark.asyncio
async def test_polling_leaves_sigterm_and_sigint_to_uvicorn(monkeypatch) -> None:
    """uvicorn installs its handlers with signal.signal; aiogram replaced them unless told not to."""

    def uvicorn_handler(signum, frame) -> None:  # pragma: no cover - never called
        raise AssertionError("not meant to be called")

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 - aiogram's signature
        raise TelegramUnauthorizedError(method=method, message="Unauthorized")

    monkeypatch.setattr(AiohttpSession, "make_request", make_request)
    monkeypatch.setattr(bot_module, "log", _Recorder())
    handled = (signal.SIGTERM, signal.SIGINT)
    previous = {sig: signal.signal(sig, uvicorn_handler) for sig in handled}
    loop = asyncio.get_running_loop()
    try:
        await bot_module.bot_loop(WELL_FORMED_TOKEN)
        after = {sig: signal.getsignal(sig) for sig in handled}
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)

    assert after == {sig: uvicorn_handler for sig in handled}, after


@pytest.mark.asyncio
async def test_a_dead_background_task_does_not_stop_the_shutdown_cleanup(
    committed_conn, monkeypatch
) -> None:
    """`committed_conn` makes the suite create the test database that the lifespan connects to."""
    closed: list[str] = []
    real_close_pool = app_main.close_pool

    async def close_pool() -> None:
        closed.append("pool")
        await real_close_pool()

    import app.utils.redis as redis_utils

    real_close_redis = redis_utils.close_redis

    async def close_redis() -> None:
        closed.append("redis")
        await real_close_redis()

    async def dead_bot(token: str) -> None:
        raise RuntimeError("the bot task died at once")

    monkeypatch.setattr(app_main, "close_pool", close_pool)
    monkeypatch.setattr(redis_utils, "close_redis", close_redis)
    monkeypatch.setattr(bot_module, "bot_loop", dead_bot)
    monkeypatch.setattr(settings, "telegram_bot_token", WELL_FORMED_TOKEN)
    monkeypatch.setattr(settings, "run_background_jobs", True)

    with TestClient(app_main.create_app()):
        pass  # entering and leaving runs the lifespan

    assert closed == ["redis", "pool"]
