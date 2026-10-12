"""Password hashing and secret sealing.

Passwords and API keys are hashed with argon2id (memory-hard, deliberately
slow) — a plain SHA-256 of a low-entropy password is a rainbow table waiting to
happen, and a leaked DB should not hand over partner credentials.

The webhook subscription secret is a different animal: we need it back to sign
outgoing deliveries, so it cannot be hashed. It is sealed instead —
authenticated encryption with a key the database does not have. A leaked DB
yields ciphertext only; the key lives in configuration.

Both halves keep the ability to recognise a legacy SHA-256 digest so existing
rows can be upgraded on the next login instead of every partner being forced
through a reset: a hash that has to be re-derived to be checked is checked, and
then replaced in place.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import weakref
from collections.abc import Callable

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.config.settings import settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# Legacy Slice-0 digests are 64-char hex SHA-256. Recognised by shape so the
# verifier can fall back to checking them, then upgrade the row.
_LEGACY_HEX_LEN = 64
_HEX = set("0123456789abcdef")

_hasher = PasswordHasher(
    # Defaults are the OWASP-recommended lane; left explicit so a library bump
    # cannot silently make every login slower or every hash unverifiable.
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)
_DUMMY_PASSWORD_HASH = _hasher.hash(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))


# One argon2 run holds 64 MiB. Several at once are fine, a flood of them is not.
_MAX_CONCURRENT_HASHES = max(2, min(4, os.cpu_count() or 2))
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


async def run_hashing[T](fn: Callable[..., T], /, *args: object) -> T:
    """Run a hash or a verification off the event loop, a few at a time.

    argon2 takes about 70 ms and 64 MiB a call. On the loop, a burst of failed logins
    froze every other request for as long as the burst lasted (measured: /healthz at
    0.7-1.6 s during 24 concurrent logins); in a thread, the loop keeps serving.
    """
    loop = asyncio.get_running_loop()
    slots = _slots.get(loop)
    if slots is None:
        slots = _slots[loop] = asyncio.Semaphore(_MAX_CONCURRENT_HASHES)
    async with slots:
        return await asyncio.to_thread(fn, *args)


def dummy_secret_hash() -> str:
    """Return the process-local Argon2 digest used for unknown accounts."""
    return _DUMMY_PASSWORD_HASH


def _is_legacy_digest(value: str) -> bool:
    return len(value) == _LEGACY_HEX_LEN and all(c in _HEX for c in value)


def _verify_dummy(raw: str) -> None:
    try:
        _hasher.verify(_DUMMY_PASSWORD_HASH, raw)
    except (VerificationError, ValueError):
        pass


def hash_secret(raw: str) -> str:
    """Hash a password or API key with argon2id.

    A fresh salt per call, so the same plaintext never lands as the same row.
    """
    return _hasher.hash(raw)


def verify_secret(raw: str, stored: str) -> bool:
    """Check a plaintext against a stored digest.

    Accepts an argon2 hash or a legacy SHA-256 digest — the caller decides what
    to do with a legacy hit (re-hash it, see needs_rehash).
    """
    if _is_legacy_digest(stored):
        matched = hmac.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), stored)
        if not matched:
            _verify_dummy(raw)
        return matched
    try:
        return _hasher.verify(stored, raw)
    except VerifyMismatchError:
        return False
    except (VerificationError, ValueError):
        # A malformed or truncated hash column is not something to paper over:
        # treat it as a failed login, and let the caller's audit trail see it.
        log.warning("malformed-hash-verify")
        _verify_dummy(raw)
        return False


def needs_rehash(stored: str) -> bool:
    """A legacy SHA-256 digest, or an argon2 hash weaker than current params.

    Call this only after verify_secret matched — never hash first and ask later.
    """
    if _is_legacy_digest(stored):
        return True
    try:
        return _hasher.check_needs_rehash(stored)
    except (VerificationError, ValueError):
        return False


# ---- sealed storage --------------------------------------------------------


def _fernet(key: str | None = None) -> Fernet:
    value = key if key is not None else settings.webhook_seal_key
    if not value:
        raise RuntimeError(
            "WEBHOOK_SEAL_KEY is not configured — webhook secrets cannot be "
            "stored without a seal key. Set it in .env (see .env.example)."
        )
    return Fernet(value.encode())


def seal(plaintext: str) -> str:
    """Encrypt a secret the database must not read back.

    Returns a Fernet token: versioned, authenticated, time-stamped. The same
    plaintext seals to a different token every call, so the column reveals
    nothing about which subscriptions share a secret.
    """
    return _fernet().encrypt(plaintext.encode()).decode()


def unseal(token: str) -> str:
    """Decrypt a sealed secret. Raises if neither key works or data was tampered.

    Falls back to webhook_seal_key_previous so a rotation is not a delivery
    outage: the outgoing key stays readable until the last row is re-sealed.
    """
    keys = [settings.webhook_seal_key, settings.webhook_seal_key_previous]
    errors = []
    for key in keys:
        if not key:
            continue
        try:
            return _fernet(key).decrypt(token.encode()).decode()
        except (InvalidToken, ValueError) as exc:
            errors.append(exc)
    raise RuntimeError("webhook secret could not be unsealed") from (errors[-1] if errors else None)


def seal_key_is_configured() -> bool:
    """Whether a seal key is present, for a startup check that fails closed."""
    return bool(settings.webhook_seal_key)


def unseals_with_current_key(token: str) -> bool:
    """Whether the current key alone reads this token.

    A rotation walks rows sealed under the outgoing key; this tells it which
    rows still need re-sealing and which it can leave alone.
    """
    if not settings.webhook_seal_key:
        return False
    try:
        _fernet(settings.webhook_seal_key).decrypt(token.encode())
    except (InvalidToken, ValueError):
        return False
    return True


def _example_key() -> str:
    """A fresh key for .env.example generation and developer nudges."""
    return base64.urlsafe_b64encode(Fernet.generate_key()).decode()
