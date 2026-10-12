"""What the deployment files promise an operator, checked where no CI job builds the image.

docker-compose.yml hands the containers only the variables it names, so a key that .env.example documents and the
compose file does not is ignored without a word: the operator sets it, nothing changes, and no log line says so.
The same files carry two settings that were measured on the running stack and that nothing else guards: bounded
logs, and the malloc setting that gives a photo decode's memory back.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

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


def test_every_service_rotates_its_logs() -> None:
    """Docker's default json-file driver keeps every line until the container is removed.

    75 bytes per request were measured, with the iCal feed's token among them (docs/DEPLOY.md): on a
    host that runs for months that is a full disk, and tokens that outlive their rotation.
    """
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    assert set(services) == {"db", "redis", "app", "jobs"}, (
        "a service was added: give it a log limit"
    )
    unbounded = []
    for name, service in services.items():
        logging = service.get("logging") or {}
        options = logging.get("options") or {}
        if logging.get("driver") != "json-file" or not (
            options.get("max-size") and options.get("max-file")
        ):
            unbounded.append(name)
    assert not unbounded, f"no max-size and max-file for: {unbounded}"


def test_the_image_gives_decoded_photo_memory_back() -> None:
    """A 39.7 MP photo decode left each worker at ~440 MiB for good (140 -> 877 MiB for the app container).

    Pillow takes 16 MiB blocks; glibc raises its mmap threshold to that size after the first free and then
    serves them from the heap, which is not returned. A fixed threshold makes every large block an mmap, and
    the container went back to ~150 MiB after the same uploads.
    """
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^ENV MALLOC_MMAP_THRESHOLD_=\d+$", dockerfile, re.M), (
        "MALLOC_MMAP_THRESHOLD_ must be set in the image, see the measurement in docs/DEPLOY.md"
    )
