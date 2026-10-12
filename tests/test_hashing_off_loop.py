"""Password and API-key hashing never runs on the event loop, and never floods the machine."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.modules.admin import service as admin_service
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import LoginRequest
from app.utils import secrets as secrets_util


@pytest.mark.asyncio
async def test_run_hashing_leaves_the_loop_thread() -> None:
    loop_thread = threading.get_ident()

    worker_thread = await secrets_util.run_hashing(threading.get_ident)

    assert worker_thread != loop_thread


@pytest.mark.asyncio
async def test_run_hashing_returns_the_result_and_passes_the_error_on() -> None:
    assert await secrets_util.run_hashing(pow, 2, 10) == 1024
    with pytest.raises(ZeroDivisionError):
        await secrets_util.run_hashing(divmod, 1, 0)


@pytest.mark.asyncio
async def test_run_hashing_runs_only_a_few_at_a_time() -> None:
    limit = secrets_util._MAX_CONCURRENT_HASHES
    lock = threading.Lock()
    running = peak = 0

    def work() -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.03)
        with lock:
            running -= 1

    await asyncio.gather(*(secrets_util.run_hashing(work) for _ in range(limit * 4)))

    assert 1 <= peak <= limit


def test_the_slots_follow_each_event_loop() -> None:
    """The semaphore is per loop: a second loop (tests, reloads) must not trip over the first."""
    for _ in range(3):
        assert asyncio.run(secrets_util.run_hashing(pow, 3, 3)) == 27


@pytest.mark.asyncio
async def test_a_failed_partner_login_verifies_off_the_loop(monkeypatch, db_conn) -> None:
    seen: list[int] = []
    real = auth_service.verify_secret

    def spy(raw: str, stored: str) -> bool:
        seen.append(threading.get_ident())
        return real(raw, stored)

    monkeypatch.setattr(auth_service, "verify_secret", spy)

    with pytest.raises(ValueError, match="invalid credentials"):
        await auth_service.login(
            db_conn, LoginRequest(email="nobody@example.com", password="x" * 12)
        )

    assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_a_failed_admin_login_verifies_off_the_loop(monkeypatch, db_conn) -> None:
    seen: list[int] = []
    real = admin_service.verify_secret

    def spy(raw: str, stored: str) -> bool:
        seen.append(threading.get_ident())
        return real(raw, stored)

    monkeypatch.setattr(admin_service, "verify_secret", spy)

    with pytest.raises(ValueError, match="invalid credentials"):
        await admin_service.login_admin(db_conn, "nobody@example.com", "x" * 12)

    assert seen and threading.get_ident() not in seen
