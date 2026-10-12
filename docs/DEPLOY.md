# Docker Compose deployment

The Compose stack runs PostgreSQL 16, Redis 7, the HTTP API, and one dedicated background-jobs process. The API serves the frontend built into the image. PostgreSQL and Redis are only reachable on the private Compose network; the API is published on the host.

## Prepare secrets and start

Copy the example environment file, then set the required values. Generate a distinct random value for each secret; do not reuse the development placeholders or commit .env.

    cp .env.example .env
    openssl rand -hex 32
    uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Use a generated value of at least 32 characters for JWT_SECRET, the Fernet value for WEBHOOK_SEAL_KEY, and a separate random password for POSTGRES_PASSWORD. Keep the default POSTGRES_USER and POSTGRES_DB unless there is a reason to change them. APP_ENV is fixed to production by Compose so startup configuration checks remain active.

Then run:

    docker compose up --build -d
    docker compose ps
    docker compose logs -f app jobs

Set APP_BASE_URL to the externally visible HTTPS origin. WEB_PORT controls the published host port; the container listens on port 8000. Compose refuses to start if JWT_SECRET, WEBHOOK_SEAL_KEY, or POSTGRES_PASSWORD is empty.

## Environment

| Variable | Required/default | Purpose |
| --- | --- | --- |
| POSTGRES_USER | hotel_user | PostgreSQL role created by the database image. |
| POSTGRES_DB | hotel_mp | Database created by the database image. |
| POSTGRES_PASSWORD | Required | Database credential; use a generated value. |
| JWT_SECRET | Required | Application signing key; use a unique random value of at least 32 characters. |
| WEBHOOK_SEAL_KEY | Required | Fernet key used to seal partner webhook secrets. Generate with the command above. |
| WEBHOOK_SEAL_KEY_PREVIOUS | Empty | Optional old Fernet key during a key rotation. |
| APP_BASE_URL | required | Public https origin used in partner-facing URLs (iCal feed addresses). Startup refuses http and localhost. |
| APP_PORT | 8000 | Internal Uvicorn port; Compose keeps it fixed and WEB_PORT changes the published host port. |
| WEB_PORT | 8000 | Host port published by the API service. |
| WEB_CONCURRENCY | 2 | Uvicorn workers per API container. The jobs service always uses one. |
| FORWARDED_ALLOW_IPS | 127.0.0.1 | Uvicorn peers allowed to supply forwarded client addresses. |
| LOG_LEVEL | info | Application log level. |

## Web workers, jobs, and proxy trust

The app service sets RUN_BACKGROUND_JOBS=false and may use multiple Uvicorn workers. The jobs service sets RUN_BACKGROUND_JOBS=true and WEB_CONCURRENCY=1. Keep exactly one jobs service: each process runs the hold reaper, iCal poller, outbox and retention loops, and the optional Telegram long-poll bot. Scaling jobs would duplicate these loops and can start multiple Telegram pollers.

Set WEB_CONCURRENCY to fit the database connection budget; each web worker opens its own pool. To scale API containers behind a reverse proxy, remove the direct host port publication and route traffic to the app service over a private Compose network.

Rate limits use the client address reported by Uvicorn. The image enables --proxy-headers but trusts X-Forwarded-For only from FORWARDED_ALLOW_IPS. Leave the safe default when connecting directly. If a reverse proxy is used, set this variable to only that proxy's fixed address or its isolated network range, and prevent untrusted clients from reaching the API directly. Do not set it to *: any peer could forge a client address and bypass per-address limits.

## Persistent data and backups

PostgreSQL data is stored in the postgres_data named volume. Uploaded property photos are stored in the media_data volume mounted at /app/media. Keep both volumes when replacing containers; docker compose down -v deletes them.

Create a database dump and copy the photo directory to a separate backup location:

    docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > database-$(date +%F).sql
    docker compose cp app:/app/media ./media-backup

Back up both data sets on a schedule and test restores before relying on them. A database dump does not include uploaded photos.

## Checking a running system

- `scripts/check_invariants.py` is read-only (one REPEATABLE READ snapshot) and safe against production:
  no hold or sold count that disagrees with the bookings behind it, no charge without a booking or a
  return, no refund made twice, commission and outbox events that match the booking states, and a
  warning when the hold reaper is behind. Exit status 1 means a violation; `--json` for a scheduler.
  Run it from cron and alert on a non-zero exit. `--self-test` proves the checker itself.
- `scripts/chaos_bookings.py` seeds random guests, a reaper, stop-sells and (in `inproc` mode) a
  payment provider that fails on purpose, then runs the checker. It writes fake data: it refuses a
  database whose name does not contain chaos, test, scratch or tmp. Use a throwaway database.
- `scripts/load_holds.py` fires N simultaneous holds at a running server on a room it creates and
  removes (`--n 100 --units 1` must give one 201 and 99 409s). It refuses when `APP_ENV` is not a
  development environment.
- The log events that need a human are listed in `docs/ARCHITECTURE.md` section 4.8
  (`charge-not-returned`, `payment-unrecorded`, `outbox-emit-failed`, ...).
- `/readyz` answers 503 when the database or Redis is down, which is what the container healthchecks
  use; `/healthz` is liveness only.

## Not verified

- Docker is not installed in the development sandbox, so the image was not built and the Compose stack was not started.
- Container healthchecks, reverse-proxy networking, persistent-volume ownership, and backup/restore commands were not exercised against Docker.
- The Dockerfile and Compose configuration received static checks only; they still need a real Docker build and deployment smoke test.

## What startup refuses

`APP_ENV` defaults to `production`, so a deployment that forgets it is checked as one. Unless
`APP_ENV` is `local`, `dev` or `test`, the application stops at boot, listing every problem at once, when:

- `JWT_SECRET` is the placeholder from the repository or shorter than 32 characters (anyone who knows
  the placeholder could sign an admin token);
- `WEBHOOK_SEAL_KEY` (or `WEBHOOK_SEAL_KEY_PREVIOUS`, when set) is not a valid Fernet key;
- `ALLOW_PRIVATE_TARGETS` is on (it lets partner-supplied webhook and iCal URLs reach the internal network);
- `APP_BASE_URL` is not the public `https` address;
- `PAYMENT_MODE` is `fail` or `delayed_stub` (test-only), or has no implementation.

`PAYMENT_MODE=stub` is accepted, since it is the only implemented provider, and logs a warning at every
start: bookings are confirmed without charging anyone.
