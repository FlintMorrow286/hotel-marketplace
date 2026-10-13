"""Startup checks: settings that must be right before the app takes traffic.

Run from the lifespan, so a bad deploy fails at boot with the reason, not on the
first login or the first payment.
"""

from __future__ import annotations

import ipaddress
from email.utils import parseaddr
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

MAIL_BACKENDS = {"log", "smtp"}

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


def _payout_rule_problems() -> list[str]:
    """The payout rule lives in app.config.legal, not in Settings; a typo there fails at boot."""
    from app.modules.payout.service import rule_problems

    return rule_problems()


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

    problems.extend(_payout_rule_problems())

    if s.mail_backend not in MAIL_BACKENDS:
        problems.append(f"MAIL_BACKEND={s.mail_backend!r} is not one of {sorted(MAIL_BACKENDS)}")
    elif s.mail_backend == "smtp":
        if not s.smtp_host.strip():
            problems.append("SMTP_HOST must be set when MAIL_BACKEND=smtp")
        if "@" not in parseaddr(s.mail_from)[1]:
            problems.append("MAIL_FROM must hold an address, as in: Name <no-reply@example.com>")
        if s.smtp_starttls and s.smtp_ssl:
            problems.append(
                "SMTP_STARTTLS and SMTP_SSL exclude each other: STARTTLS upgrades a plain "
                "connection (587), SSL is TLS from the first byte (465)"
            )
        if bool(s.smtp_user) != bool(s.smtp_password):
            problems.append("SMTP_USER and SMTP_PASSWORD go together")

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
    if s.mail_backend == "smtp" and s.smtp_user and not (s.smtp_starttls or s.smtp_ssl):
        problems.append(
            f"SMTP_USER is set but neither SMTP_STARTTLS nor SMTP_SSL is: the password would "
            f"cross the network in clear text {where}"
        )

    return problems


def check_config(s: Settings) -> None:
    problems = production_problems(s)
    if problems:
        raise ConfigError("; ".join(problems))
