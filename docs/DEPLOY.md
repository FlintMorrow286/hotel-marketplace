# Docker Compose deployment

The Compose stack runs PostgreSQL 16, Redis 7, the HTTP API, and one dedicated background-jobs process. The API serves the frontend built into the image. PostgreSQL and Redis are only reachable on the private Compose network; the API is published on the host.

## Prepare secrets and start

Copy the example environment file, then set the required values. Generate a distinct random value for each secret; do not reuse the development placeholders or commit .env.

    cp .env.example .env
    openssl rand -hex 32
    uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Use a generated value of at least 32 characters for JWT_SECRET, the Fernet value for WEBHOOK_SEAL_KEY, and a separate random password for POSTGRES_PASSWORD. Compose puts that password into DATABASE_URL as it is, so use letters and digits (`openssl rand -hex 32` is fine): a `/`, `#`, `?` or `@` makes the URL unreadable (with a `/` or `#`, `app` and `jobs` restart with `ValueError: invalid literal for int()` although the database itself accepted the password), and a `%` followed by two hex digits is decoded into another character. Keep the default POSTGRES_USER and POSTGRES_DB unless there is a reason to change them. APP_ENV is fixed to production by Compose so startup configuration checks remain active. The copied `.env.example` sets `LOG_LEVEL=debug`, which is for development: set `LOG_LEVEL=info`.

Then run:

    docker compose up --build -d
    docker compose ps
    docker compose logs -f app jobs

Set APP_BASE_URL to the externally visible HTTPS origin. WEB_PORT controls the published host port; the container listens on port 8000. Compose refuses to start if JWT_SECRET, WEBHOOK_SEAL_KEY, or POSTGRES_PASSWORD is empty.

## First staff account

Nothing creates the first staff (admin) account: `scripts/seed_demo.py` does, but with a password that is in
the repository, and it is not in the image. Staff moderate properties, read the commission report and are the
only ones who can change a rate plan's commission (`PATCH /v1/admin/rate-plans/{id}`), so create one account
after the first start. The password is read from the environment, not the command line, and stored as an
argon2 hash:

```sh
printf 'Staff password: '; read -rs ADMIN_PASSWORD; echo; export ADMIN_PASSWORD
docker compose exec -T -e ADMIN_PASSWORD app python - staff@example.com <<'PY'
import asyncio
import os
import sys

import asyncpg

from app.config.settings import settings
from app.utils.secrets import hash_secret


async def main(email: str, password: str) -> None:
    conn = await asyncpg.connect(settings.database_url)
    await conn.execute(
        "INSERT INTO admin (email, password_hash) VALUES ($1, $2)",
        email,
        hash_secret(password),
    )
    await conn.close()


password = os.environ["ADMIN_PASSWORD"]
if len(password) < 12:
    sys.exit("the password must be at least 12 characters")
asyncio.run(main(sys.argv[1], password))
PY
unset ADMIN_PASSWORD
```

Staff sign in at `/admin/login`. Running it again with the same email fails on the unique key.

## Environment

| Variable | Required/default | Purpose |
| --- | --- | --- |
| POSTGRES_USER | hotel_user | PostgreSQL role created by the database image. |
| POSTGRES_DB | hotel_mp | Database created by the database image. |
| POSTGRES_PASSWORD | Required | Database credential, letters and digits only (`openssl rand -hex 32`): Compose puts it into `DATABASE_URL` unencoded. |
| JWT_SECRET | Required | Application signing key; use a unique random value of at least 32 characters. |
| WEBHOOK_SEAL_KEY | Required | Fernet key used to seal partner webhook secrets. Generate with the command above. |
| WEBHOOK_SEAL_KEY_PREVIOUS | Empty | Optional old Fernet key during a key rotation (see "Rotating WEBHOOK_SEAL_KEY"). |
| APP_BASE_URL | required | Public https origin used in partner-facing URLs (iCal feed addresses). Startup refuses http and localhost. |
| APP_PORT | 8000 | Internal Uvicorn port; Compose keeps it fixed and WEB_PORT changes the published host port. |
| WEB_PORT | 8000 | Host port published by the API service. |
| WEB_CONCURRENCY | 2 | Uvicorn workers per API container. The jobs service always uses one. |
| FORWARDED_ALLOW_IPS | 127.0.0.1 | Uvicorn peers allowed to supply forwarded client addresses. |
| LOG_LEVEL | info | Application log level. `.env.example` sets `debug` for development; set `info` in a deployment. |
| API_DOCS | unset | Unset means off in production. `true` serves Swagger UI (`/docs`), ReDoc and `/openapi.json`, which list every endpoint and load their scripts from a CDN. |
| LOGIN_LIMIT_PER_MIN, REGISTER_LIMIT_PER_MIN, LOOKUP_LIMIT_PER_MIN, HOLD_LIMIT_PER_MIN, AVAILABILITY_LIMIT_PER_MIN, SEARCH_LIMIT_PER_MIN, PHOTO_UPLOAD_LIMIT_PER_MIN, ICAL_SYNC_LIMIT_PER_MIN | 10, 5, 10, 20, 120, 240, 20, 6 | Requests per client address per minute. While Redis is down each Uvicorn worker counts for itself, so the ceiling becomes the limit times `WEB_CONCURRENCY`. |
| HOLD_TTL_MIN | 15 | Minutes a hold keeps the inventory. `app` and `jobs` get the same value from here. |
| ACCESS_TOKEN_TTL_MIN, JWT_ALGORITHM | 60, HS256 | Lifetime of partner and staff sessions in minutes; token algorithm. |
| TELEGRAM_BOT_TOKEN | unset | Starts the partner notification bot in the `jobs` process. |
| SENTRY_DSN | unset | Error reporting. |
| PAYMENT_MODE, ALLOW_PRIVATE_TARGETS | stub, false | Production refuses to start with any other payment mode or with private targets on, and says so. |

The containers get every key of `.env.example` and nothing else from `.env`; the lines Compose fixes itself (`APP_ENV`, `APP_HOST`, `APP_PORT`, `DATABASE_URL`, `REDIS_URL`, `MEDIA_DIR`, `RUN_BACKGROUND_JOBS`) win over the file. `tests/test_compose_env.py` fails when a key is added to `.env.example` and not to `docker-compose.yml`. A variable left unset keeps the application's default.

## Web workers, jobs, and proxy trust

The app service sets RUN_BACKGROUND_JOBS=false and may use multiple Uvicorn workers. The jobs service sets RUN_BACKGROUND_JOBS=true and WEB_CONCURRENCY=1. Keep exactly one jobs service: each process runs the hold reaper, iCal poller, outbox and retention loops, and the optional Telegram long-poll bot. Scaling jobs would duplicate these loops and can start multiple Telegram pollers.

Every process opens its own pool of 5 to 25 connections (`app/db/pool.py`): each web worker and the jobs process. The default stack (2 workers and jobs) can therefore hold up to 75 of PostgreSQL's default 100 `max_connections` (3 of them reserved for superusers). Raise `max_connections` before raising WEB_CONCURRENCY or adding `app` replicas: at 4 workers (5 processes) a busy instance can ask for 125 and fail with `too many clients`. Connect to PostgreSQL directly or through a pooler in session mode: migrations, the hold queue and the payment and refund lock take session-level advisory locks, which transaction pooling does not keep on one connection. To scale API containers behind a reverse proxy, remove the direct host port publication and route traffic to the app service over a private Compose network.

Rate limits use the client address reported by Uvicorn. The image enables --proxy-headers but trusts X-Forwarded-For only from FORWARDED_ALLOW_IPS. Leave the safe default when connecting directly. If a reverse proxy is used, set this variable to only that proxy's fixed address or its isolated network range, and prevent untrusted clients from reaching the API directly. Do not set it to *: any peer could forge a client address and bypass per-address limits.

## Proxy on the same host

A reverse proxy on the Docker host does not reach the container from 127.0.0.1. The container sees it, like any other
connection made from the host, as coming from the Compose network's gateway (for example `172.19.0.1`). With the
default `FORWARDED_ALLOW_IPS=127.0.0.1` Uvicorn therefore ignores `X-Forwarded-For`, and every user falls into one
bucket per limit and gets 429 together. Find the gateway and put it into `.env`:

```sh
docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' "$(docker compose ps -q app)"
```

Docker chooses the subnet when it creates the project's network, and `docker compose down` removes that network, so a
later `up` may choose another. To keep the address fixed, give the network its own subnet in
`docker-compose.override.yml`, which Compose reads next to `docker-compose.yml` without a flag:

```yaml
networks:
  default:
    ipam:
      config:
        - subnet: 172.30.0.0/24
          gateway: 172.30.0.1
```

and set `FORWARDED_ALLOW_IPS=172.30.0.1`. Trust only that address, and keep untrusted clients away from the published
port, as above.

## Persistent data and backups

PostgreSQL data is stored in the postgres_data named volume. Uploaded property photos are stored in the media_data volume mounted at /app/media. Keep both volumes when replacing containers; docker compose down -v deletes them.

Create a database dump and copy the photo directory to a separate backup location:

    docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > database-$(date +%F).sql
    docker compose cp app:/app/media ./media-backup

Back up both data sets on a schedule and test restores before relying on them. A database dump does not include uploaded photos.

## Rotating WEBHOOK_SEAL_KEY

`WEBHOOK_SEAL_KEY` seals the secrets of partners' webhook subscriptions; if it is lost they cannot be read. The
image ships `scripts/rotate_seal_key.py` to change it without stopping deliveries:

1. Generate a new Fernet key. In `.env` move the current value to `WEBHOOK_SEAL_KEY_PREVIOUS` and put the new key
   in `WEBHOOK_SEAL_KEY`.
2. `docker compose up -d` recreates `app` and `jobs` with both keys; the old one stays readable as the fallback.
3. Re-seal every secret under the new key:

   ```sh
   docker compose exec -T app python -m scripts.rotate_seal_key
   ```

   It prints how many rows it re-sealed and is safe to run again. Exit status 2 means some rows were sealed with a
   key that is in neither variable: those secrets are lost and the subscriptions have to be created again.
4. When it says everything readable is on the current key, empty `WEBHOOK_SEAL_KEY_PREVIOUS` in `.env` and run
   `docker compose up -d` again.

## Checking a running system

- `scripts/check_invariants.py` is read-only (one REPEATABLE READ snapshot) and safe against production:
  no hold or sold count that disagrees with the bookings behind it, no charge without a booking or a
  return, no refund made twice, commission and outbox events that match the booking states, and a
  warning when the hold reaper is behind. Exit status 1 means a violation; `--json` for a scheduler.
  Run it from cron and alert on a non-zero exit. `--self-test` proves the checker itself.
  In the Compose deployment it runs inside the app container, which ships this script and not the
  chaos and load ones: `docker compose exec -T app python -m scripts.check_invariants`.
- `scripts/chaos_bookings.py` seeds random guests, a reaper, stop-sells and (in `inproc` mode) a
  payment provider that fails on purpose, then runs the checker. It writes fake data: it refuses a
  database whose name does not contain chaos, test, scratch or tmp. Use a throwaway database.
- `scripts/load_holds.py` fires N simultaneous holds at a running server on a room it creates and
  removes (`--n 100 --units 1` must give one 201 and 99 409s). It refuses when `APP_ENV` is not a
  development environment.
- The log events that need a human are listed in `docs/ARCHITECTURE.md` section 4.8
  (`charge-not-returned`, `payment-unrecorded`, `outbox-emit-failed`, ...).
- `/readyz` answers 503 when the database is down, which is what the container healthchecks use. A Redis
  outage answers 200 with `"status": "degraded"` and `"redis": "error"`: Redis only backs the rate
  limiter, which counts in memory while it is away, so an outage must not take every instance out of
  rotation. `/healthz` is liveness only.

## What was exercised

On a Docker host (2 CPU, Compose v2), with the files as shipped:

- `docker compose up --build -d --wait`: the image builds once (`app` builds it, `jobs` runs the same one) and
  `app`, `jobs`, `db` and `redis` report healthy.
- `docker compose up -d --wait` without `--build` on a host that has never built the image builds it once and starts all four services healthy (28 s with a warm build cache). `app` and `jobs` start together on an empty database and both come up. With no `.env`, Compose refuses with `Set POSTGRES_PASSWORD in .env`.
- Uploaded photos survive `docker compose down` and `up` (the `media_data` volume, written by the non-root
  user) and are served as `image/webp`.
- With the database stopped `/readyz` answers 503 and the container turns unhealthy; when the database is back
  it recovers without a restart.
- With Redis stopped under a running server (a local process, not the container stack): `/readyz` answers 200
  `degraded`, search keeps working, and the login limiter keeps its ceiling in memory (ten wrong logins pass, the
  next ones get 429); when Redis is back `/readyz` is `ok` again without a restart.
- The backup commands above: the `pg_dump` output restores into an empty database with the same tables and row
  counts, and `docker compose cp` copies the photo directory.
- `docker compose exec -T app python -m scripts.check_invariants` reports no violations; `API_DOCS=true` serves
  `/docs` and `/openapi.json`; without it those paths answer with the web app's own page.
- The staff account command above, as written: it stores one argon2 hash; `POST /v1/admin/login` answers 200
  with an admin token for the right password and 401 for a wrong one or an unknown email; the token is accepted
  by `GET /v1/admin/reports/commission` (200) and `PATCH /v1/admin/rate-plans/{id}` (404 for an unknown plan,
  401 without a token); a second run with the same email fails on the unique key, and a password under 12
  characters is refused before anything is written.
- The seal key rotation above, as written, on the Compose stack built from this checkout: a webhook secret created
  through the API is a Fernet token that only the first key reads; after the keys are swapped (the first one in
  `WEBHOOK_SEAL_KEY_PREVIOUS`) and `docker compose up -d`, `rotate_seal_key` reports one row re-sealed (exit 0) and
  only the new key reads the token; a second run reports it already on the current key and leaves the token as it
  is; with `WEBHOOK_SEAL_KEY_PREVIOUS` emptied the stack is healthy (`/readyz` 200) and the script still exits 0;
  a secret sealed by a key that is in neither variable is reported as unsealable, exit 2.
- On the Compose stack (two workers) with Redis stopped, `/readyz` answers 200 `degraded` and 30 wrong logins give
  20 answers of 401 and then 10 of 429: each worker counts its own ten. About a second after Redis is back `/readyz`
  is `ok` again. With the database stopped `/readyz` answers 503 (`/healthz` stays 200) and about two seconds after
  it is back it answers 200 with the data intact.
- Which `.env` values reach the containers, in four runs. A copy of `.env.example` with `LOGIN_LIMIT_PER_MIN=3`,
  `HOLD_LIMIT_PER_MIN=7` and `HOLD_TTL_MIN=5` gives `app` and `jobs` those values (three wrong logins pass, the fourth
  gets 429) while `APP_ENV=local` and `APP_PORT=8002` in the same file do not change the fixed `production` and
  8000; blank `TELEGRAM_BOT_TOKEN` and `SENTRY_DSN` start fine. With only the four required values none of the
  tunables is set in the containers and the default limit of 10 applies. `PAYMENT_MODE=tinkoff`,
  `PAYMENT_MODE=fail` and `ALLOW_PRIVATE_TARGETS=true` each stop `app` and `jobs` at boot with a `ConfigError`
  that names the setting.
- Behind a proxy on the Docker host, simulated by requests from the host to the published port with a hand-set
  `X-Forwarded-For`: with the default `FORWARDED_ALLOW_IPS` five logins gave `401 401 401 429 429` and one Redis key,
  `rl:login:172.19.0.1`, the Compose network's gateway; with that gateway, and with the network's subnet in CIDR form
  (`172.19.0.0/16`), in `FORWARDED_ALLOW_IPS` they gave five `401` on five keys (`rl:login:203.0.113.1` to `.5`). The
  commands of "Proxy on the same host" were run as written: the gateway is printed correctly, and with the fixed
  subnet from `docker-compose.override.yml` the network is `172.30.0.0/24`, gateway `172.30.0.1`, before and after
  `docker compose down` and `up`.
- `POSTGRES_PASSWORD` and the `DATABASE_URL` that Compose builds from it. With `ab/cd` `app` and `jobs` restart with
  `ValueError: invalid literal for int() with base 10: 'ab'` while the database accepted the password; `ab+cd=ef` and a
  64-character hex password come up healthy. asyncpg's own DSN parser, run on the URL for other passwords: `+`, `=`
  and `:` are read back as typed; `#` gives the same `ValueError` as `/`, `?` gives `bad query field`, `@` makes the
  host `cd@db` and the password `ab`, and `%41` becomes `A`. 48% of 10,000 samples of `openssl rand -base64 32`
  contained a `/`.
- The URL guard in the running containers, with the production settings (`ALLOW_PRIVATE_TARGETS` off). From the `app`
  container `https://example.com` is reached (200, through the pinned transport) and these are refused: `db`, `redis`,
  `app` and `jobs` by name (their Compose addresses are private), `127.0.0.1`, `localhost`, `::1`, `::ffff:127.0.0.1`,
  `0.0.0.0`, `169.254.169.254`, `2130706433` and `0x7f.0.0.1` (`0177.0.0.1` is rejected as an invalid address, a
  `file://` URL as not http(s)). `POST /v1/partner/webhooks` answers 422 `url must point to a public address` for `db`,
  `redis`, `169.254.169.254` and `localhost`. A subscription that is already stored with `http://redis:6379/` is tried by
  `jobs` within about 20 s and fails with `redis points at a non-public address`, which lands in
  `webhook_delivery.error`, `outbox_event.last_error`, `webhook_subscription.last_error` and the
  `outbox-delivery-failed` log line (the URL there has no credentials or query).
- A signed delivery from `jobs`, with `APP_ENV=local` and `ALLOW_PRIVATE_TARGETS=true` only so that a receiver on the
  Docker host, a private address, is reachable. The secret is stored as a Fernet token that does not contain it. The
  receiver got a `POST` with a valid `X-Signature` (HMAC-SHA256 of the body), `Idempotency-Key` equal to the event id,
  `X-Event-Type` and the JSON event; it answered 500, `jobs` retried after about 15 s (the poll interval) with the same
  key and a valid signature, got 204, and the event became `published` with a `success` row in `webhook_delivery`.
- A receiver that answers 307 with a `Location` on another host is not followed: `redirect refused: target must stay on
  the same host and port`. A redirect inside the same host and port is followed. The previous build followed both, and
  the other host received the `X-Signature` header and the whole body.

## Not verified

- A real reverse proxy in front (nginx, Caddy, Traefik): trusted-proxy handling through a real hop, TLS termination,
  HSTS over real HTTPS. Only requests made from the host were exercised (above).
- Several `app` replicas, or `WEB_CONCURRENCY` above 2, under load. `scripts/load_holds.py` refuses a production
  environment, so the hold queue was measured outside the production container.
- Restoring a backup on another host.
- Real payments: `PAYMENT_MODE=stub` is the only implemented provider.

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
