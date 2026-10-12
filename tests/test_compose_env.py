"""docker-compose.yml hands the containers only the variables it names, so a key that
.env.example documents and the compose file does not is ignored without a word: the
operator sets it, nothing changes, and no log line says so."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _keys(text: str) -> list[str]:
    return re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M)


def test_every_key_of_env_example_reaches_the_containers() -> None:
    keys = _keys((ROOT / ".env.example").read_text())
    assert "JWT_SECRET" in keys and "LOGIN_LIMIT_PER_MIN" in keys, "the key pattern matches nothing"

    compose = (ROOT / "docker-compose.yml").read_text()
    missing = [
        key
        for key in keys
        # An environment entry, or a ${KEY...} interpolation (the keys Compose uses itself).
        if not re.search(r"^\s+" + key + r":|\$\{" + key + r"[}:?-]", compose, re.M)
    ]
    assert not missing, f"in .env.example but not passed by docker-compose.yml: {missing}"
