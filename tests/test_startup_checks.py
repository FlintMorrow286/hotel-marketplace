"""Startup checks and optional dependencies.

A bad deploy should fail at boot with the reason, and the app must start without
the optional Telegram extra.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.config.checks import DEFAULT_JWT_SECRET, ConfigError, check_config
from app.config.settings import Settings, settings
from app.modules.payment.provider import get_payment_provider

GOOD_SECRET = "k" * 40


def _settings(**overrides) -> Settings:
    # _env_file=None: the checks are about these values, not the developer's .env.
    return Settings(_env_file=None, **overrides)


def test_local_may_use_the_placeholder_secret() -> None:
    check_config(_settings(app_env="local", jwt_secret=DEFAULT_JWT_SECRET))


@pytest.mark.parametrize("secret", [DEFAULT_JWT_SECRET, "short-secret"])
def test_production_refuses_a_weak_jwt_secret(secret: str) -> None:
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        check_config(_settings(app_env="production", jwt_secret=secret))


def test_production_accepts_a_long_random_secret() -> None:
    check_config(_settings(app_env="production", jwt_secret=GOOD_SECRET))


@pytest.mark.parametrize("env", ["local", "production"])
def test_an_unimplemented_payment_mode_is_refused_everywhere(env: str) -> None:
    with pytest.raises(ConfigError, match="PAYMENT_MODE"):
        check_config(_settings(app_env=env, jwt_secret=GOOD_SECRET, payment_mode="tinkoff"))


@pytest.mark.parametrize("mode", ["stub", "delayed_stub", "fail"])
def test_implemented_payment_modes_pass(mode: str) -> None:
    check_config(_settings(app_env="production", jwt_secret=GOOD_SECRET, payment_mode=mode))


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(ConfigError) as exc:
        check_config(_settings(app_env="production", payment_mode="tinkoff"))
    assert "JWT_SECRET" in str(exc.value) and "PAYMENT_MODE" in str(exc.value)


def test_provider_does_not_fall_back_to_the_stub(monkeypatch) -> None:
    """tinkoff has no implementation: quietly charging nobody would confirm bookings for free."""
    monkeypatch.setattr(settings, "payment_mode", "tinkoff")
    with pytest.raises(ValueError, match="tinkoff"):
        get_payment_provider()


def test_app_starts_without_the_telegram_extra() -> None:
    """aiogram is optional: a plain `uv sync` must still give an app that imports and builds."""
    code = (
        "import sys; sys.modules['aiogram'] = None  # makes `import aiogram` fail\n"
        "import app.main\n"
        "app.main.create_app()\n"
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert result.stdout.strip().endswith("ok")
