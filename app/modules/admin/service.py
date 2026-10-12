"""Admin service: staff authentication + property moderation.

Moderation model (Phase 1): partners may self-publish (tested behavior), so the
admin queue is oversight, not a gate. Admins see every property by status and
can block / unblock; pending_moderation is the formal submission state that
'approve' resolves. Commission is read-only here - it is snapshotted at
confirmation time and never recomputed (see report.py).
"""

from __future__ import annotations

import asyncpg

from app.modules.auth.jwt import create_access_token
from app.utils.logger import get_logger
from app.utils.secrets import (
    dummy_secret_hash,
    hash_secret,
    needs_rehash,
    run_hashing,
    verify_secret,
)

log = get_logger(__name__)

PropertyStatus = str


async def login_admin(conn: asyncpg.Connection, email: str, password: str) -> str:
    """Verify staff credentials, return an admin-scoped JWT.

    Legacy SHA-256 hashes verify and are upgraded in place, same as partner
    login — staff does not get a forced reset either.
    """
    row = await conn.fetchrow("SELECT id::text, password_hash FROM admin WHERE email = $1", email)
    if row is None:
        await run_hashing(verify_secret, password, dummy_secret_hash())
        raise ValueError("invalid credentials")
    if not await run_hashing(verify_secret, password, row["password_hash"]):
        raise ValueError("invalid credentials")

    if needs_rehash(row["password_hash"]):
        await conn.execute(
            "UPDATE admin SET password_hash = $2 WHERE id = $1",
            row["id"],
            await run_hashing(hash_secret, password),
        )
        log.info("password-rehashed", scope="admin")

    log.info("admin-login", admin_id=row["id"])
    return create_access_token(row["id"], scope="admin")


async def list_properties_for_admin(
    conn: asyncpg.Connection, status: str | None = None
) -> list[dict]:
    """Every property with its owner's email - the moderation queue."""
    args: list = []
    where = ""
    if status:
        args.append(status)
        where = "WHERE p.status = $1"
    rows = await conn.fetch(
        f"""
        SELECT p.id::text              AS id,
               p.name                   AS name,
               p.property_type          AS property_type,
               p.city                   AS city,
               p.status                 AS status,
               p.created_at             AS created_at,
               pt.email                 AS partner_email
        FROM property p
        JOIN partner  pt ON pt.id = p.partner_id
        {where}
        ORDER BY
            CASE p.status
                WHEN 'pending_moderation' THEN 0
                WHEN 'published'          THEN 1
                WHEN 'draft'              THEN 2
                ELSE                           3
            END,
            p.created_at DESC
        """,
        *args,
    )
    return [dict(r) for r in rows]


# Allowed transitions under admin control. Anything else is a client error.
_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"published", "blocked"},
    "pending_moderation": {"published", "draft", "blocked"},
    "published": {"blocked"},
    "blocked": {"published"},
}


async def set_property_status(
    conn: asyncpg.Connection, property_id: str, new_status: str
) -> dict | None:
    """Move a property to new_status. Returns the row, or None if not found.

    Raises ValueError on an illegal transition so the route can answer 409
    instead of silently 'succeeding'.
    """
    current = await conn.fetchval("SELECT status FROM property WHERE id = $1", property_id)
    if current is None:
        return None
    if new_status not in _TRANSITIONS.get(current, set()):
        raise ValueError(f"illegal transition: {current} -> {new_status}")

    row = await conn.fetchrow(
        """
        UPDATE property p SET status = $2 WHERE p.id = $1 AND p.status = $3
        RETURNING p.id::text, p.name, p.property_type, p.city, p.status,
                  p.created_at,
                  (SELECT pt.email FROM partner pt WHERE pt.id = p.partner_id)
                      AS partner_email
        """,
        property_id,
        new_status,
        current,
    )
    if row is None:
        latest = await conn.fetchval("SELECT status FROM property WHERE id = $1", property_id)
        if latest is None:
            return None
        raise ValueError(f"concurrent status transition: {current} -> {latest}")

    from app.modules.outbox import service as outbox_service

    await outbox_service.emit(
        conn,
        aggregate="property",
        aggregate_id=property_id,
        event_type=outbox_service.PROPERTY_STATUS_CHANGED,
        payload={"property_id": property_id, "name": row["name"], "status": new_status},
        property_id=property_id,
    )
    log.info("property-moderated", property_id=property_id, status=new_status)
    return dict(row)
