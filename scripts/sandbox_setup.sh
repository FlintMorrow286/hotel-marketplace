#!/usr/bin/env bash
# Setup for a fresh clone or sandbox: Python and web dependencies, a .env with generated
# secrets, and PostgreSQL + Redis (local, or in Docker where there are none). Idempotent: it
# changes nothing that already exists, so it is also what brings the services back after a restart.
#
#     bash scripts/sandbox_setup.sh
#
# A failing step does not stop the others; the exit status says whether any failed.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

status=0

echo "== python dependencies"
# A cache left by another user (root in an earlier sandbox) is read-only here: use a private one.
uv_cache="${UV_CACHE_DIR:-$HOME/.cache/uv}"
mkdir -p "$uv_cache" 2>/dev/null || true
if [[ ! -w "$uv_cache" ]]; then
    export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache-$(id -u)"
    echo "uv cache $uv_cache is not writable; using $UV_CACHE_DIR"
fi
uv sync --extra bot || status=1

echo "== web dependencies"
if [[ -d web/node_modules ]]; then
    echo "web/node_modules exists, skipping npm ci"
else
    (cd web && npm ci --no-audit --no-fund) || status=1
fi

echo "== .env"
if [[ -f .env ]]; then
    echo ".env exists, leaving it alone"
else
    cp .env.example .env
    seal_key="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    jwt_secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    python3 - "$seal_key" "$jwt_secret" <<'PY' || status=1
import re
import sys

seal_key, jwt_secret = sys.argv[1:3]
text = open(".env").read()
text = re.sub(r"(?m)^WEBHOOK_SEAL_KEY=.*$", lambda _: f"WEBHOOK_SEAL_KEY={seal_key}", text, count=1)
text = re.sub(r"(?m)^JWT_SECRET=.*$", lambda _: f"JWT_SECRET={jwt_secret}", text, count=1)
open(".env", "w").write(text)
PY
    chmod 600 .env
    echo "created .env from .env.example with a generated WEBHOOK_SEAL_KEY and JWT_SECRET"
fi

echo "== PostgreSQL and Redis"
bash scripts/start_services.sh || status=1

exit "$status"
