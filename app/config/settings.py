"""Application configuration.

All settings come from environment / .env. No magic constants here —
legal/business parameters live in `app/config/legal.py` and `app/config/payment.py`.
"""

import os
import urllib.parse
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _test_sibling(database_url: str) -> str:
    """The `_test` sibling of a DSN: same server and credentials, other database.

    Only the path (the database name) changes; query strings (sslmode and the
    like) are carried over untouched.
    """
    split = urllib.parse.urlsplit(database_url)
    if not split.path or split.path == "/":
        raise ValueError("DATABASE_URL has no database name — cannot derive a test database")
    return urllib.parse.urlunsplit(split._replace(path=f"{split.path}_test"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----- App -----
    app_env: str = Field(default="local")
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    # Public origin used to build URLs handed to partners (iCal feeds, links).
    app_base_url: str = Field(default="http://localhost:8000")
    log_level: str = Field(default="info")

    # ----- Database -----
    database_url: str = "postgresql://hotel_user:hotel_pass@localhost:5432/hotel_mp"

    # ----- Redis -----
    redis_url: str = Field(default="redis://localhost:6379/0")

    # ----- Auth -----
    jwt_secret: str = Field(default="CHANGE_ME_SUPER_SECRET_32_PLUS_CHARS_LONG")
    jwt_algorithm: str = Field(default="HS256")
    access_token_ttl_min: int = Field(default=60)

    # ----- Booking core -----
    hold_ttl_min: int = Field(default=15)

    # ----- Channel sync -----
    # How often the iCal importer revisits a subscribed calendar.
    ical_sync_interval_sec: int = Field(default=900)

    # ----- Channel API rate limits -----
    # Per API key, separate buckets: a read burst is a nuisance, a write burst
    # is real bookings. Over the limit the channel gets 429 + Retry-After.
    channel_write_limit_per_min: int = Field(default=30)
    channel_read_limit_per_min: int = Field(default=120)
    # The window both buckets live in. Separate from the numbers so a test does
    # not have to sleep a whole minute.
    rate_limit_window_sec: int = Field(default=60)

    # ----- Public endpoints that take guesses -----
    # Attempts per client address per window; one account or booking code gets
    # 3x that across all addresses (app/utils/ratelimit_http.py). Raise them if
    # many users legitimately share an address.
    login_limit_per_min: int = Field(default=10)
    register_limit_per_min: int = Field(default=5)
    lookup_limit_per_min: int = Field(default=10)
    # Per webhook subscription: the worker never pushes a partner's hook faster
    # than this, so our queue spike cannot drown their server.
    webhook_rate_per_sec: int = Field(default=10)
    # How often the outbox worker claims a batch. Exposed for tests.
    outbox_poll_interval_sec: int = Field(default=15)

    # ----- Outbox backlog limit -----
    # A mass operation (bulk price load, an iCal import of a whole season) can
    # throw thousands of events at the queue; the worker drains it at
    # webhook_rate_per_sec per subscription. Beyond this depth, low-priority
    # events are shed instead of queued — see app/modules/outbox/backlog.py.
    # Bookings are never subject to this.
    outbox_max_pending: int = Field(default=2000)
    # How deep the queue has to be before the admin strip flags it as falling
    # behind, in seconds since the oldest pending event.
    outbox_lag_alert_sec: int = Field(default=300)

    # ----- Outbox sharding -----
    # The queue is sliced into this many logical shards by the property the
    # event is about (hashtext % this, computed in SQL). Each delivery worker
    # owns a disjoint subset, so delivery parallelises and one partner's burst
    # never sits in another's claim order. Changing this needs a re-backfill of
    # the shard column — old rows keep their value, so nothing breaks, only the
    # spread goes uneven.
    outbox_shard_count: int = Field(default=16)
    # Concurrent delivery loops. Worker k owns shards s where s % this == k.
    # SKIP LOCKED keeps claiming correct even if these overlap.
    outbox_workers: int = Field(default=4)

    # ----- Sealing -----
    # Webhook subscription secrets must be readable back to sign outgoing
    # deliveries, so they are sealed (authenticated encryption) rather than
    # hashed. The DB holds ciphertext only; this key is the difference between
    # a leaked table and a leaked secret. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Rotation re-seals every existing secret: keep the outgoing key in
    # webhook_seal_key_previous and unseal falls back to it, so deliveries keep
    # working while scripts/rotate_seal_key.py walks the rows. Drop the previous
    # key once the script reports nothing left on the old one.
    webhook_seal_key: str = Field(default="")
    # The outgoing key, readable only. Empty except during a rotation.
    webhook_seal_key_previous: str = Field(default="")

    # ----- Webhook delivery retention -----
    # webhook_delivery is partitioned by delivered_at, one partition a month,
    # and ages out on a schedule (app/modules/outbox/retention.py) — never in
    # a request, never wholesale.
    # Successes are the bulk of the table and only the proof of a delivery:
    # the booking itself is the source of truth, so they go first.
    webhook_delivery_success_days: int = Field(default=30)
    # Failures are kept far longer — this is the table reconciliation reads
    # when a partner claims a booking never arrived.
    webhook_delivery_failed_days: int = Field(default=365)
    # Partitions are created this many months ahead so a delivery never waits
    # on missing DDL.
    webhook_delivery_partition_ahead_months: int = Field(default=3)
    # How often the retention sweep runs (also tops up future partitions).
    retention_interval_sec: int = Field(default=3600)

    # ----- Payment -----
    payment_mode: str = Field(default="stub")

    # ----- Property photos -----
    # Partners upload photos through the cabinet; the catalog renders them.
    # Files live outside web/ (never copied into public/) and are served from
    # this prefix by a static mount. The URL is built from the prefix, so a CDN
    # swap only changes these two.
    media_dir: Path = Field(default=PROJECT_ROOT / "media")
    media_url_prefix: str = Field(default="/media")
    # One upload is capped per file, not per request: a resize happens in a
    # threadpool, so a huge raw file still costs CPU before it is rejected.
    photo_max_bytes: int = Field(default=10 * 1024 * 1024)
    photo_max_per_property: int = Field(default=12)
    # Long edge of the stored full size; the card/thumb pair is generated next.
    photo_max_dimension: int = Field(default=1920)
    photo_thumb_width: int = Field(default=800)

    # ----- Telegram -----
    telegram_bot_token: str = Field(default="")

    # ----- Sentry -----
    sentry_dsn: str = Field(default="")

    @property
    def is_dev(self) -> bool:
        return self.app_env in ("local", "dev", "test")

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def test_dsn(self) -> str:
        """DSN the test suite connects to.

        The suite must not wipe the developer's database: a `committed_conn`
        test DELETEs every row in every table, so running pytest used to
        destroy the demo content the frontend is developed against. Unless
        that is explicitly asked for, the tests get the `_test` sibling of
        DATABASE_URL — a database they own and can drop.

        Set PYTEST_DISABLE_TEST_DB=1 to write to DATABASE_URL directly; only
        do that when it really is a throwaway database.
        """
        if os.environ.get("PYTEST_DISABLE_TEST_DB") == "1":
            return self.database_url
        return _test_sibling(self.database_url)

    @property
    def pool_dsn(self) -> str:
        """DSN the app's connection pool uses.

        Production and local dev use DATABASE_URL. Under pytest the app pool
        must point at `test_dsn`, not DATABASE_URL: the suite seeds its own
        throwaway database, and a `TestClient` booted inside a test would
        otherwise read a different database from the one the test wrote to —
        every HTTP test would fail with a 404.

        PYTEST_RUNNING is set by the suite itself (conftest), never by
        production code, so a deployed entry point cannot land here by
        accident.
        """
        if os.environ.get("PYTEST_RUNNING") == "1":
            return self.test_dsn
        return self.database_url


settings = Settings()
