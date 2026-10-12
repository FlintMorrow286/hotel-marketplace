FROM node:22-bookworm-slim AS frontend

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /bin/uv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    APP_PORT=8000 \
    WEB_CONCURRENCY=2 \
    FORWARDED_ALLOW_IPS=127.0.0.1

# Decoding a 39.7 MP photo left each worker at ~440 MiB for good (the container: 140 -> 877 MiB, measured):
# Pillow takes 16 MiB blocks, and after the first free glibc serves blocks of that size from the heap, which
# is not returned. A fixed threshold makes every large block an mmap; the container went back to ~150 MiB.
ENV MALLOC_MMAP_THRESHOLD_=131072

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra bot --no-install-project \
    && rm -rf /root/.cache/uv

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app app \
    && mkdir -p /app/media \
    && chown app:app /app/media

COPY --chown=app:app app/ ./app/
COPY --chown=app:app migrations/ ./migrations/
# The read-only integrity checker, for `docker compose exec app python -m scripts.check_invariants`.
COPY --chown=app:app scripts/check_invariants.py ./scripts/check_invariants.py
# The webhook seal key rotation (docs/DEPLOY.md), for `docker compose exec app python -m scripts.rotate_seal_key`.
COPY --chown=app:app scripts/rotate_seal_key.py ./scripts/rotate_seal_key.py
COPY --from=frontend --chown=app:app /web/dist/ ./web/dist/

USER app

HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('APP_PORT', '8000') + '/readyz', timeout=2).read()"]

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${APP_PORT:-8000}\" --workers \"${WEB_CONCURRENCY:-2}\" --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
