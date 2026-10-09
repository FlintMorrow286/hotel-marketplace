"""Hand-written SQL migration runner.

Migrations are plain `.sql` files in `migrations/`, applied in filename order.
Applied migrations are tracked in the `schema_migrations` table.

Usage:
    python -m app.db.migrate            # apply all pending
    python -m app.db.migrate --check    # exit 1 if pending (for CI)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.config.settings import settings
from app.utils.logger import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


async def _list_migrations() -> list[tuple[int, Path]]:
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        stem = path.stem.split("__")[0]
        try:
            num = int(stem)
        except ValueError:
            log.warning("skip-migration-bad-name", path=str(path))
            continue
        out.append((num, path))
    return out


async def _ensure_tracking_table(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    integer PRIMARY KEY,
            filename   text    NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


async def run_migrations(check_only: bool = False, dsn: str | None = None) -> list[int]:
    """Apply all pending migrations. Returns list of applied version numbers.

    `dsn` selects the database; it defaults to DATABASE_URL. The test suite
    passes its own throwaway database so it never has to migrate the dev one.
    """
    import asyncpg

    applied: list[int] = []
    migrations = await _list_migrations()

    conn = await asyncpg.connect(dsn=dsn or settings.database_url)
    try:
        if not check_only:
            # The app runs migrations on every start, so two instances coming up
            # together (a rolling deploy) would apply the same file twice. The
            # session-level lock is held until this connection closes: the second
            # runner waits, then finds nothing pending. A check only reads.
            await conn.execute("SELECT pg_advisory_lock(hashtextextended('app.db.migrate', 0))")
        await _ensure_tracking_table(conn)
        rows = await conn.fetch("SELECT version FROM schema_migrations")
        done = {r["version"] for r in rows}

        pending = [(n, p) for n, p in migrations if n not in done]

        if check_only:
            if pending:
                log.info("pending-migrations", count=len(pending))
                return [n for n, _ in pending]
            return []

        for num, path in pending:
            sql = path.read_text(encoding="utf-8")
            log.info("apply-migration", version=num, file=path.name)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, filename) VALUES ($1, $2)",
                    num,
                    path.name,
                )
            applied.append(num)

        log.info("migrations-done", applied=applied, total=len(migrations))
    finally:
        await conn.close()

    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply SQL migrations")
    parser.add_argument("--check", action="store_true", help="only report pending migrations")
    args = parser.parse_args()

    applied = asyncio.run(run_migrations(check_only=args.check))
    if args.check and applied:
        print(f"Pending migrations: {applied}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
