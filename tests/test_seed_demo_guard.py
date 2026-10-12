"""seed_demo plants accounts with published passwords, so it refuses a real environment."""

from __future__ import annotations

import sys

import pytest

from app.config.settings import settings
from scripts import seed_demo


class _DatabaseReached(Exception):
    """Stands in for the database: raised when the guard let the seed through."""


@pytest.fixture(autouse=True)
def _no_database(monkeypatch) -> None:
    async def init_pool(*args, **kwargs):
        raise _DatabaseReached

    monkeypatch.setattr(seed_demo, "init_pool", init_pool)


@pytest.mark.asyncio
@pytest.mark.parametrize("app_env", ["production", "staging"])
async def test_refuses_outside_a_development_environment(monkeypatch, capsys, app_env) -> None:
    monkeypatch.setattr(settings, "app_env", app_env)
    monkeypatch.setattr(sys, "argv", ["seed_demo.py"])

    assert await seed_demo.main() == 2

    assert f"APP_ENV={app_env!r}" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("app_env", ["local", "dev", "test"])
async def test_development_environments_go_ahead(monkeypatch, app_env) -> None:
    monkeypatch.setattr(settings, "app_env", app_env)
    monkeypatch.setattr(sys, "argv", ["seed_demo.py"])

    with pytest.raises(_DatabaseReached):
        await seed_demo.main()


@pytest.mark.asyncio
async def test_the_flag_overrides_the_refusal(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(sys, "argv", ["seed_demo.py", "--allow-any-database"])

    with pytest.raises(_DatabaseReached):
        await seed_demo.main()
