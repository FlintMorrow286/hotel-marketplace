"""Auth service: registers partners, issues JWTs.

Slice 0 keeps this intentionally simple — password hash + token. Partner
profile fields (legal_type, phone…) come in Slice 1.

All functions take an `asyncpg.Connection` so tests can pass a transaction
that is rolled back afterwards.
"""

from __future__ import annotations

import asyncpg

from app.modules.auth.jwt import create_access_token
from app.modules.auth.schemas import LoginRequest, PartnerRegisterRequest
from app.utils.logger import get_logger
from app.utils.secrets import dummy_secret_hash, hash_secret, needs_rehash, verify_secret

log = get_logger(__name__)


async def register_partner(conn: asyncpg.Connection, data: PartnerRegisterRequest) -> str:
    """Create a partner row, return new partner id (uuid string)."""
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO partner (email, password_hash, name)
            VALUES ($1, $2, $3)
            RETURNING id::text
            """,
            data.email,
            hash_secret(data.password),
            data.name,
        )
        return row["id"]
    except asyncpg.UniqueViolationError as exc:
        raise ValueError("email already registered") from exc


async def login(conn: asyncpg.Connection, data: LoginRequest) -> str:
    """Verify credentials, return JWT.

    A legacy SHA-256 password still verifies, but is upgraded to argon2 in the
    same statement — the plaintext is only in hand for these few instructions,
    and the partner never has to reset anything.
    """
    row = await conn.fetchrow(
        "SELECT id::text, password_hash FROM partner WHERE email = $1", data.email
    )
    if row is None:
        verify_secret(data.password, dummy_secret_hash())
        raise ValueError("invalid credentials")
    if not verify_secret(data.password, row["password_hash"]):
        raise ValueError("invalid credentials")

    if needs_rehash(row["password_hash"]):
        await conn.execute(
            "UPDATE partner SET password_hash = $2 WHERE id = $1",
            row["id"],
            hash_secret(data.password),
        )
        log.info("password-rehashed", scope="partner")

    log.info("partner-login", partner_id=row["id"])
    return create_access_token(row["id"], scope="partner")
