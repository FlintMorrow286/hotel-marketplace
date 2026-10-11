import hashlib

from app.utils import secrets


def test_legacy_wrong_password_uses_one_argon2_decoy_verification(monkeypatch) -> None:
    hasher_type = type(secrets._hasher)
    original_verify = hasher_type.verify
    calls: list[tuple[str, str]] = []

    def counted_verify(hasher, stored: str, raw: str) -> bool:
        calls.append((stored, raw))
        return original_verify(hasher, stored, raw)

    monkeypatch.setattr(hasher_type, "verify", counted_verify)
    legacy_hash = hashlib.sha256(b"correct-password").hexdigest()

    assert secrets.verify_secret("wrong-password", legacy_hash) is False
    assert calls == [(secrets.dummy_secret_hash(), "wrong-password")]


def test_legacy_success_still_requires_upgrade() -> None:
    legacy_hash = hashlib.sha256(b"correct-password").hexdigest()

    assert secrets.verify_secret("correct-password", legacy_hash) is True
    assert secrets.needs_rehash(legacy_hash) is True


def test_malformed_hash_fails_without_raising() -> None:
    assert secrets.verify_secret("any-password", "malformed") is False
