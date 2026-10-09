"""FastAPI application factory.

Startup: connect DB pool + Redis, run migrations.
Shutdown: close both.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config.settings import settings
from app.db.migrate import run_migrations
from app.db.pool import close_pool, init_pool
from app.utils.logger import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info("startup", env=settings.app_env, port=settings.app_port)

    pool = await init_pool()
    log.info("db-pool-ready", size=pool.get_size())

    from app.utils.redis import init_redis

    redis_client = await init_redis()
    redis_version = (await redis_client.info("server"))["redis_version"]
    log.info("redis-ready", version=redis_version)

    applied = await run_migrations()
    log.info("migrations-applied", versions=applied)

    # Webhook secrets are sealed with this key; without it, new subscriptions
    # fail closed and legacy ones degrade to plaintext. Loud at startup rather
    # than as a 500 on the first webhook creation.
    from app.utils.secrets import seal_key_is_configured

    if not seal_key_is_configured():
        log.warning(
            "seal-key-missing",
            hint="generate one with: python -c "
            '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"',
        )

    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env)

    from app.jobs.reaper import reaper_loop
    from app.modules.bot import bot_loop
    from app.modules.outbox.retention import retention_loop
    from app.modules.outbox.worker import outbox_loop, shard_groups
    from app.modules.sync.poller import import_loop
    from app.utils.redis import close_redis

    reaper_task = asyncio.create_task(reaper_loop())
    import_task = asyncio.create_task(import_loop())
    # One delivery loop per shard group: the outbox drains in parallel, and a
    # partner's burst no longer sits in another partner's claim order.
    outbox_tasks = [asyncio.create_task(outbox_loop(group)) for group in shard_groups()]
    retention_task = asyncio.create_task(retention_loop())
    # The bot is a notification channel, not a booking dependency: it runs only
    # when a token exists, and its failure never stops the app.
    bot_task = None
    if settings.telegram_bot_token:
        bot_task = asyncio.create_task(bot_loop(settings.telegram_bot_token))
    else:
        log.warning("telegram-bot-skipped", reason="TELEGRAM_BOT_TOKEN is not set")

    yield

    reaper_task.cancel()
    import_task.cancel()
    for task in outbox_tasks:
        task.cancel()
    retention_task.cancel()
    if bot_task is not None:
        bot_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reaper_task
        await import_task
        await asyncio.gather(*outbox_tasks)
        await retention_task
        if bot_task is not None:
            await bot_task

    await close_redis()
    await close_pool()
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Hotel Marketplace API",
        version="0.1.0",
        description="B2B2C accommodation marketplace — booking core",
        docs_url="/docs",
        lifespan=lifespan,
    )

    from app.modules.admin.routes import router as admin_router
    from app.modules.auth.routes import router as auth_router
    from app.modules.booking.routes import router as booking_router
    from app.modules.bot.routes import router as bot_router
    from app.modules.channel.routes import router as channel_router
    from app.modules.inventory.routes import router as inventory_router
    from app.modules.outbox.routes import router as outbox_router
    from app.modules.payment.routes import router as payment_router
    from app.modules.property.photo_routes import router as photo_router
    from app.modules.property.routes import router as property_router
    from app.modules.property.unit_type_routes import router as unit_type_router
    from app.modules.rate.routes import router as rate_router
    from app.modules.sync.routes import router as sync_router

    app.include_router(auth_router)
    app.include_router(bot_router)
    app.include_router(property_router)
    app.include_router(photo_router)
    app.include_router(unit_type_router)
    app.include_router(inventory_router)
    app.include_router(booking_router)
    app.include_router(payment_router)
    app.include_router(rate_router)
    app.include_router(admin_router)
    app.include_router(sync_router)
    app.include_router(channel_router)
    app.include_router(outbox_router)
    # A malformed uuid in a path (`/v1/properties/not-a-uuid`) is a missing
    # resource, not a server fault — Postgres rejects the value before the
    # query runs, so it never reaches the "no such row" branch.
    from app.utils.uuid_http import install_uuid_handler

    install_uuid_handler(app)

    # A SERIALIZABLE conflict that outlasts the retries is "try again", not a fault.
    from app.db.tx import install_conflict_handler

    install_conflict_handler(app)

    # ---- single-app: serve the built frontend from one origin -------------
    # Static assets first (exact paths), then the SPA fallback so any deep
    # link (/search, /property/abc) lands on index.html for React Router.
    # Mounted LAST so it can never shadow API routes (/v1/*, /healthz, …).
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    static_dir = settings.project_root / "web" / "dist"

    if static_dir.is_dir():
        # Built frontend assets (Vite hashes these; served at /assets/*).
        app.mount(
            "/assets",
            StaticFiles(directory=static_dir / "assets"),
            name="assets",
        )

    # Partner photo uploads (written by app/modules/property/photos.py). Served
    # from a directory outside web/ — photos come through the API, never copied
    # into the frontend's public/. Created at startup so the mount is never
    # pointing at a missing directory.
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        settings.media_url_prefix,
        StaticFiles(directory=settings.media_dir),
        name="media",
    )

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict:
        """Liveness probe — no DB access, must always answer 200 fast."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> dict:
        """Readiness probe — checks DB + Redis are reachable."""
        from app.db.pool import get_pool
        from app.utils.redis import get_redis

        checks: dict = {}
        try:
            pool = get_pool()
            await pool.execute("SELECT 1")
            checks["db"] = "ok"
        except Exception as exc:
            checks["db"] = f"error: {exc}"

        try:
            redis = get_redis()
            await redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"error: {exc}"

        ok = all(v == "ok" for v in checks.values())
        return {"status": "ok" if ok else "degraded", "checks": checks}

    # SPA fallback — registered LAST, so it can never shadow the API.
    if static_dir.is_dir():
        index_html = static_dir / "index.html"

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            """Any unknown path serves index.html — client-side routing."""
            return FileResponse(index_html)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.is_dev,
        log_level=settings.log_level,
    )
