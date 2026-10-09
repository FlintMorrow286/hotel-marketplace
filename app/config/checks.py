"""Startup checks: settings that must be right before the app takes traffic.

Run from the lifespan, so a bad deploy fails at boot with the reason, not on the
first login or the first payment.
"""

from __future__ import annotations

from app.config.payment import PaymentMode
from app.config.settings import Settings

# The placeholder shipped in settings.py and .env.example. Anyone can read it in
# the repository, so a token signed with it proves nothing.
DEFAULT_JWT_SECRET = "CHANGE_ME_SUPER_SECRET_32_PLUS_CHARS_LONG"
MIN_JWT_SECRET_LEN = 32

# Modes with a provider behind them (see app/modules/payment/provider.py).
IMPLEMENTED_PAYMENT_MODES = {PaymentMode.STUB, PaymentMode.DELAYED_STUB, PaymentMode.FAIL}


class ConfigError(RuntimeError):
    """The configuration is not safe to run with."""


def check_config(s: Settings) -> None:
    problems: list[str] = []

    if s.payment_mode not in IMPLEMENTED_PAYMENT_MODES:
        problems.append(
            f"PAYMENT_MODE={s.payment_mode!r} has no implementation; "
            f"use one of {sorted(m.value for m in IMPLEMENTED_PAYMENT_MODES)}"
        )

    if not s.is_dev and (
        s.jwt_secret == DEFAULT_JWT_SECRET or len(s.jwt_secret) < MIN_JWT_SECRET_LEN
    ):
        problems.append(
            f"JWT_SECRET must be a random value of at least {MIN_JWT_SECRET_LEN} characters "
            f"when APP_ENV={s.app_env!r} (only local, dev and test may use the placeholder)"
        )

    if problems:
        raise ConfigError("; ".join(problems))
