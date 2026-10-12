#!/usr/bin/env bash
# Start PostgreSQL and Redis for development, wherever this runs: the machine's own services when
# it has them (scripts/dev_services.sh), otherwise the two containers in docker-compose.dev.yml.
#
#     bash scripts/start_services.sh
#
# Idempotent. The containers take their credentials from DATABASE_URL (environment, then .env,
# then .env.example), so the application connects with what the database was created with.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if command -v redis-server >/dev/null && command -v redis-cli >/dev/null \
    && command -v pg_isready >/dev/null && command -v psql >/dev/null; then
    exec bash scripts/dev_services.sh
fi

if ! docker info >/dev/null 2>&1; then
    echo "start_services: this machine has no local PostgreSQL/Redis tools and no Docker daemon" >&2
    exit 1
fi

eval "$(python3 - <<'PY'
import os
import re
import shlex
from urllib.parse import unquote, urlsplit

url = os.environ.get("DATABASE_URL", "")
for name in (".env", ".env.example"):
    if url:
        break
    try:
        text = open(name).read()
    except OSError:
        continue
    found = re.search(r"(?m)^DATABASE_URL=(.*)$", text)
    url = found.group(1).strip().strip("'\"") if found else ""
parts = urlsplit(url or "postgresql://hotel_user:hotel_pass@localhost:5432/hotel_mp")
values = {
    "POSTGRES_USER": unquote(parts.username or "hotel_user"),
    "POSTGRES_PASSWORD": unquote(parts.password or ""),
    "POSTGRES_DB": parts.path.lstrip("/") or "hotel_mp",
}
for key, value in values.items():
    print(f"export {key}={shlex.quote(value)}")
PY
)"

docker compose -f docker-compose.dev.yml up -d --wait
echo "PostgreSQL and Redis are running in Docker (docker-compose.dev.yml)."
