from __future__ import annotations

import hashlib

import pytest

from app.modules.admin import service as admin_service
from app.modules.auth import service as auth_service
from app.modules.auth.schemas import LoginRequest


@pytest.mark.asyncio
async def test_unknown_partner_and_admin_logins_verify_argon2_decoys(monkeypatch, db_conn) -> None:
    partner_calls: list[str] = []
    admin_calls: list[str] = []
    partner_verify = auth_service.verify_secret
    admin_verify = admin_service.verify_secret

    def count_partner_verify(raw: str, stored: str) -> bool:
        partner_calls.append(stored)
        return partner_verify(raw, stored)

    def count_admin_verify(raw: str, stored: str) -> bool:
        admin_calls.append(stored)
        return admin_verify(raw, stored)

    monkeypatch.setattr(auth_service, "verify_secret", count_partner_verify)
    monkeypatch.setattr(admin_service, "verify_secret", count_admin_verify)

    with pytest.raises(ValueError, match="invalid credentials"):
        await auth_service.login(
            db_conn,
            LoginRequest(email="missing-partner@example.com", password="secret123"),
        )
    with pytest.raises(ValueError, match="invalid credentials"):
        await admin_service.login_admin(db_conn, "missing-admin@example.com", "secret123")

    assert len(partner_calls) == 1
    assert len(admin_calls) == 1
    assert partner_calls[0].startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert admin_calls[0] == partner_calls[0]


@pytest.mark.asyncio
async def test_legacy_partner_hash_still_upgrades_after_login(db_conn) -> None:
    password = "secret123"
    email = "fix-legacy-login@example.com"
    legacy_hash = hashlib.sha256(password.encode()).hexdigest()
    partner_id = await db_conn.fetchval(
        "INSERT INTO partner (email, password_hash, name) VALUES ($1, $2, 'Tester') "
        "RETURNING id::text",
        email,
        legacy_hash,
    )

    await auth_service.login(db_conn, LoginRequest(email=email, password=password))

    stored_hash = await db_conn.fetchval(
        "SELECT password_hash FROM partner WHERE id = $1", partner_id
    )
    assert stored_hash.startswith("$argon2id$")
