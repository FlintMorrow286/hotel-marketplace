"""Startup checks: settings that must be right before the app takes traffic.

Run from the lifespan, so a bad deploy fails at boot with the reason, not on the
first login or the first payment.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from app.config.payment import PaymentMode
from app.config.settings import Settings

# The placeholder shipped in settings.py and .env.example. Anyone can read it in
# the repository, so a token signed with it proves nothing.
DEFAULT_JWT_SECRET = "CHANGE_ME_SUPER_SECRET_32_PLUS_CHARS_LONG"
MIN_JWT_SECRET_LEN = 32

# Modes with a provider behind them (see app/modules/payment/provider.py).
IMPLEMENTED_PAYMENT_MODES = {
    PaymentMode.STUB,
    PaymentMode.DELAYED_STUB,
    PaymentMode.FAIL,
    PaymentMode.MOCK_ACQUIRING,
}
# Modes that exist to exercise the code: delayed_stub waits, fail always fails.
TEST_ONLY_PAYMENT_MODES = {PaymentMode.DELAYED_STUB, PaymentMode.FAIL}

_LOCAL_SUFFIXES = ("localhost", "local", "internal")


class ConfigError(RuntimeError):
    """The configuration is not safe to run with."""


def _is_fernet_key(value: str) -> bool:
    try:
        Fernet(value.encode())
    except (ValueError, TypeError):
        return False
    return True


def _is_nonpublic_host(host: str | None) -> bool:
    if not host:
        return True
    normalized = host.rstrip(".").casefold()
    if not normalized or any(
        normalized == suffix or normalized.endswith(f".{suffix}") for suffix in _LOCAL_SUFFIXES
    ):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return any(
        (
            address.is_loopback,
            address.is_private,
            address.is_link_local,
            address.is_unspecified,
            address.is_reserved,
        )
    )


def production_problems(s: Settings) -> list[str]:
    """Everything wrong with `s`, all at once.

    A payment mode without an implementation is wrong everywhere. The rest only
    matters outside development, where APP_ENV is local, dev or test.
    """
    problems: list[str] = []

    if s.payment_mode not in IMPLEMENTED_PAYMENT_MODES:
        problems.append(
            f"PAYMENT_MODE={s.payment_mode!r} has no implementation; "
            f"use one of {sorted(m.value for m in IMPLEMENTED_PAYMENT_MODES)}"
        )

    if s.is_dev:
        return problems

    # APP_ENV defaults to production, so a deploy that forgets it lands here.
    where = f"when APP_ENV={s.app_env!r} (set APP_ENV=local to develop)"

    if s.jwt_secret == DEFAULT_JWT_SECRET or len(s.jwt_secret) < MIN_JWT_SECRET_LEN:
        problems.append(
            f"JWT_SECRET must be a random value of at least {MIN_JWT_SECRET_LEN} characters {where}"
        )
    if s.allow_private_targets:
        problems.append(
            f"ALLOW_PRIVATE_TARGETS must be false {where}: it lets partner-supplied "
            "webhook and iCal URLs reach loopback, the internal network and cloud metadata"
        )
    if not _is_fernet_key(s.webhook_seal_key):
        problems.append(f"WEBHOOK_SEAL_KEY must be a valid Fernet key {where}")
    if s.webhook_seal_key_previous and not _is_fernet_key(s.webhook_seal_key_previous):
        problems.append(f"WEBHOOK_SEAL_KEY_PREVIOUS is set but is not a valid Fernet key {where}")
    try:
        base = urlsplit(s.app_base_url)
        invalid_base_url = base.scheme != "https" or _is_nonpublic_host(base.hostname)
    except ValueError:
        invalid_base_url = True
    if invalid_base_url:
        problems.append(
            f"APP_BASE_URL must be the public https address {where}: partners are handed "
            f"iCal feed URLs built from it (got {s.app_base_url!r})"
        )
    if s.payment_mode in TEST_ONLY_PAYMENT_MODES:
        problems.append(f"PAYMENT_MODE={s.payment_mode!r} is for tests only {where}")

    return problems


def check_config(s: Settings) -> None:
    problems = production_problems(s)
    if problems:
        raise ConfigError("; ".join(problems))
