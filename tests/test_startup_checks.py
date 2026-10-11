"""Startup checks and optional dependencies.

A bad deploy should fail at boot with the reason, and the app must start without
the optional Telegram extra.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from cryptography.fernet import Fernet

from app.config.checks import DEFAULT_JWT_SECRET, ConfigError, check_config
from app.config.settings import Settings, settings
from app.modules.payment.provider import get_payment_provider

GOOD_SECRET = "k" * 40
GOOD_SEAL_KEY = Fernet.generate_key().decode()


def _settings(**overrides) -> Settings:
    # _env_file=None: the checks are about these values, not the developer's .env.
    return Settings(_env_file=None, **overrides)


def _production(**overrides) -> Settings:
    """A production configuration that is right, with `overrides` applied."""
    values = {
        "app_env": "production",
        "jwt_secret": GOOD_SECRET,
        "webhook_seal_key": GOOD_SEAL_KEY,
        "app_base_url": "https://hotels.example.com",
        "payment_mode": "stub",
        # The suite switches the guard off in the process environment; a production
        # configuration is judged on what it says, not on that.
        "allow_private_targets": False,
    }
    return _settings(**{**values, **overrides})


def test_local_may_use_the_placeholder_secret() -> None:
    check_config(_settings(app_env="local", jwt_secret=DEFAULT_JWT_SECRET))


def test_a_good_production_configuration_passes() -> None:
    check_config(_production())


@pytest.mark.parametrize("secret", [DEFAULT_JWT_SECRET, "short-secret"])
def test_production_refuses_a_weak_jwt_secret(secret: str) -> None:
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        check_config(_production(jwt_secret=secret))


def test_forgetting_app_env_means_production(monkeypatch) -> None:
    """A deploy without APP_ENV must not accept the secret printed in the repository.

    Anyone who knows it can sign an admin-scope token, and the admin routes check
    the scope in the token, not an account.
    """
    monkeypatch.delenv("APP_ENV", raising=False)
    s = _settings(jwt_secret=DEFAULT_JWT_SECRET)

    assert s.app_env == "production"
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        check_config(s)


def test_production_refuses_to_switch_the_ssrf_guard_off() -> None:
    with pytest.raises(ConfigError, match="ALLOW_PRIVATE_TARGETS"):
        check_config(_production(allow_private_targets=True))
    check_config(_settings(app_env="local", allow_private_targets=True))


@pytest.mark.parametrize("key", ["", "not-a-key", "AAAA"])
def test_production_needs_a_valid_seal_key(key: str) -> None:
    with pytest.raises(ConfigError, match="WEBHOOK_SEAL_KEY must"):
        check_config(_production(webhook_seal_key=key))


def test_an_outgoing_seal_key_must_be_valid_too() -> None:
    with pytest.raises(ConfigError, match="WEBHOOK_SEAL_KEY_PREVIOUS"):
        check_config(_production(webhook_seal_key_previous="garbage"))
    check_config(_production(webhook_seal_key_previous=Fernet.generate_key().decode()))


@pytest.mark.parametrize(
    "url",
    [
        "http://hotels.example.com",
        "https://localhost:8000",
        "https://127.0.0.1",
        "hotels.example.com",
        "http://localhost:8000",
    ],
)
def test_production_needs_the_public_https_address(url: str) -> None:
    with pytest.raises(ConfigError, match="APP_BASE_URL"):
        check_config(_production(app_base_url=url))


@pytest.mark.parametrize("mode", ["delayed_stub", "fail"])
def test_test_only_payment_modes_are_refused_in_production(mode: str) -> None:
    with pytest.raises(ConfigError, match="tests only"):
        check_config(_production(payment_mode=mode))
    check_config(_settings(app_env="test", payment_mode=mode))


@pytest.mark.parametrize("env", ["local", "production"])
def test_an_unimplemented_payment_mode_is_refused_everywhere(env: str) -> None:
    with pytest.raises(ConfigError, match="PAYMENT_MODE"):
        check_config(_settings(app_env=env, jwt_secret=GOOD_SECRET, payment_mode="tinkoff"))


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(ConfigError) as exc:
        check_config(
            _settings(app_env="production", payment_mode="tinkoff", allow_private_targets=True)
        )
    message = str(exc.value)
    for name in (
        "JWT_SECRET",
        "PAYMENT_MODE",
        "ALLOW_PRIVATE_TARGETS",
        "WEBHOOK_SEAL_KEY",
        "APP_BASE_URL",
    ):
        assert name in message


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
