#!/usr/bin/env bash
# Preview: services up, SPA built, schema migrated, demo content seeded, then the API serving
# both the API and the built SPA on APP_PORT (default 8002).
#
#     bash scripts/sandbox_run.sh
#
# Demo logins (see scripts/seed_demo.py): partner demo@example.com, admin admin@example.com.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

bash scripts/dev_services.sh

if [[ ! -f web/dist/index.html ]]; then
    (cd web && npm run build)
fi

uv run python -m app.db.migrate
uv run python scripts/seed_demo.py

exec uv run uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT:-8002}"
