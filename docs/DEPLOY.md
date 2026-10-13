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
| TELEGRAM_BOT_TOKEN | unset | Starts the partner notification bot in the `jobs` process, which long-polls api.telegram.org (outbound HTTPS from the container). A token that is malformed or that Telegram rejects is logged once as `bot-polling-failed` (error level) and the bot stays off; nothing else in `jobs` is affected. |
| SENTRY_DSN | unset | Error reporting. An error carries its exception, stack trace and request line; local variables, request bodies, the `X-Booking-Token` header, the iCal feed token in the URL and the log lines before it are not sent (`app/utils/sentry.py`). Error-level log lines are sent as events, with their text. |
| PAYMENT_MODE | stub | `stub` confirms a booking at once; `mock_acquiring` sends the guest to a test payment page and back (see Payments below). Neither charges anyone, and each logs `payment-stub-outside-dev` at every start outside development. `fail` and `delayed_stub` are test-only, `tinkoff` has no implementation: production refuses all three. |
| ALLOW_PRIVATE_TARGETS | false | Production refuses to start with private targets on, and says so. |
| MAIL_BACKEND | log | `log` delivers nothing: every letter is kept in the `email_message` table (admin → "Письма") and the booking pages do not promise one. `smtp` delivers through the server below. Production runs on `log` with a startup warning (`email-not-delivered-outside-dev`) until there is an account. |
| MAIL_FROM, MAIL_REPLY_TO | `Бронирование <no-reply@localhost>`, empty | The sender (`Name <address>`; the address is also the envelope sender and the domain of each `Message-ID`) and where replies go. Empty reply address: the letters say replies are not read. |
| SMTP_HOST, SMTP_PORT | empty, 587 | The server for `MAIL_BACKEND=smtp`. Startup refuses `smtp` without a host. |
| SMTP_USER, SMTP_PASSWORD | empty | The account; both or neither. Production refuses an account without `SMTP_STARTTLS` or `SMTP_SSL` (the password would cross the network in clear text). |
| SMTP_STARTTLS, SMTP_SSL | true, false | STARTTLS upgrades a plain connection (port 587); SSL is TLS from the first byte (port 465). Not both. |

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

## Memory and logs

Measured on the Compose stack with `WEB_CONCURRENCY=2` (container memory, MiB): idle `app` 140, `jobs` 126, `db` 129,
`redis` 3, so about 400 for the stack. A photo at the pixel limit (`PHOTO_MAX_PIXELS`, 40 MP) takes about 360 in the
worker that decodes it and 1.6 s; each worker decodes one photo at a time, so with six uploads at once the `app`
container peaked at 1.09 GiB. Plan for at least 2 GiB for the whole stack, and about 450 more for every worker you
add (`WEB_CONCURRENCY`). Compose sets no memory limits.

The image sets `MALLOC_MMAP_THRESHOLD_=131072`. Without it each worker kept about 440 for good after its first such
decode: `app` went from 140 to 877 and stayed there (it did not grow over 30 more uploads, so this is not a leak, but
a host sized from the idle figure runs out). With it `app` is back at 150 after the same uploads; the peak is the same.

Every service keeps its last five log files of 20 MB (`logging` in `docker-compose.yml`). Docker's default driver
keeps every line until the container is removed: 75 bytes per request were measured, which is 124 MiB a day at 20
requests a second. `docker compose logs` shows only what is still on disk; ship the logs elsewhere for more history.

## Persistent data and backups

PostgreSQL data is stored in the postgres_data named volume. Uploaded property photos are stored in the media_data volume mounted at /app/media. Keep both volumes when replacing containers; docker compose down -v deletes them.

Back up both data sets on a schedule, to a separate location, and keep a copy of `.env` with them: the dump holds the
webhook secrets sealed with `WEBHOOK_SEAL_KEY` (lost, they cannot be read, see "Rotating WEBHOOK_SEAL_KEY") and the
booking tokens are keyed from `JWT_SECRET`. A database dump does not include uploaded photos.

    docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > database-$(date +%F).sql
    docker compose exec -T app tar -C /app/media -cf - . > media-$(date +%F).tar

Restore into an empty database, before `app` and `jobs` start: on their first start they create the schema, and a dump
restored over it fails halfway (measured: 98 errors, the bookings missing, `psql` still exiting 0). On a new host, or
after `docker compose down -v`, with the same `.env`:

    docker compose up -d --wait db
    docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" "$POSTGRES_DB"' < database-YYYY-MM-DD.sql
    docker compose up -d --wait
    docker compose exec -T -u root app tar -C /app/media -xf - < media-YYYY-MM-DD.tar
    docker compose exec -T app python -m scripts.check_invariants

Where the database already exists, run `docker compose stop app jobs` first, then
`docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'`,
and continue from the `psql` line.

Restore the photos with `tar` as root, as above, not with `docker compose cp`. `cp` gives the files the uid of whoever
ran it, and the application's user then cannot write to those property directories: uploading a photo to such a
property answers 415 "the file is corrupt" (the `photo-decode-failed` log line says `Permission denied`), and deleting
one succeeds while its files stay on disk, still served at their URL. A second scheduled `cp` of the same directory
also puts its copy inside the first one (`media-backup/media/...`) and leaves the first as it was.

A restored copy that is not the production system must not run `jobs` with production's `.env`: it would deliver the
webhooks still pending in the outbox, poll the partners' iCal feeds and compete with production for the Telegram bot.
Start it with `docker compose up -d --wait db app`, which leaves `jobs` out.

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
- Logs hold URLs. uvicorn's access log, and the access log of any reverse proxy in front, list every request path, and
  the iCal feed's only credential is in its path (`/v1/ical/<token>.ics`: a calendar client cannot send a header). The
  token is therefore in `docker compose logs app` and in the proxy's log, and whoever can read those can read the
  feed, which lists the booking codes and the dates of the unit type. A partner changes the token with
  `POST /v1/partner/unit-types/{id}/ical-feed` and the old address stops working at once. Keep these logs out of
  shipping to third parties, or blank that path where they are shipped. Sentry does not get it (`SENTRY_DSN` above).
  Compose keeps only the last five 20 MB files of each service's log (see "Memory and logs").

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
- The backup and restore of "Persistent data and backups", on a second Compose project (new volumes, the same `.env`)
  on the same Docker host, from a stack with two partners, a confirmed booking and a photo. The dump restored into the
  empty database with `ON_ERROR_STOP` without an error and with the source's row counts, the stack then applied no
  migration (`"versions": []`), the partner could log in, `check_invariants` exited 0, and after the `tar` restore the
  photo came back byte for byte, owned by the application's uid; a new photo uploaded (201) and the restored one was
  deleted with both its files (404 afterwards). The same dump restored over the started stack was refused (exit 3,
  nothing changed); without `ON_ERROR_STOP` it printed 98 errors, loaded the partners and properties but not the
  bookings, and exited 0. Recovery on the same host (stop, `dropdb`, `createdb`, restore, start) took three partners
  back to the two of the dump. With `docker compose cp` instead of `tar` the files belonged to the host user, an upload
  answered 415 (`[Errno 13] Permission denied` in the log), and a delete answered 200 and left both files on disk,
  still served with 200; `docker compose cp ./dir app:/app/media` put the copy under `/app/media/dir`, where the photo
  answered 404. `docker compose up -d --wait db app` starts `app`, `db` and `redis` only: `app` logs
  `background-jobs-skipped`, and no bot starts even with `TELEGRAM_BOT_TOKEN` set.
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
- What reaches Sentry, measured on the production stack with `SENTRY_DSN` pointing at a stand-in endpoint on the
  Docker host and the database stopped, so that a login, a booking lookup and a pay call with `X-Booking-Token` fail
  with 500. Each produced one event (`gaierror`). With the SDK's defaults, which the application used before
  `app/utils/sentry.py`, the events held the password, the guest's e-mail, the booking token, the JWT of the caller and
  the partner's e-mail as plain text: in frame variables (`data`, `body`, `body_bytes`, `scope`, `request`), in the
  request body and in the request headers. Now none of them appears and the same three events arrive. The unit tests
  fail real routes the same way and fail if any of the four options is removed.
- The same measurement for the iCal feed, whose credential is in the URL path: with the database stopped, `GET
  /v1/ical/<token>.ics` answers 500 and the event carried the token in `request.url` (the SDK's scrubber does not read
  the URL, and the route template in `transaction` is harmless). `before_send` now sends `/v1/ical/[Filtered].ics`; the
  feed still answers 200 when the database is up. A test reads the OpenAPI paths of the real application and fails
  for a route with a `token`, `secret`, `password` or `api_key` path parameter that no pattern in `app/utils/sentry.py`
  covers. The token does stay in uvicorn's access log (one line per request, measured), see "Checking a running
  system".
- The Telegram bot in `jobs`, with `TELEGRAM_BOT_TOKEN` set in `.env` (three runs, then `docker compose stop`). No token:
  `telegram-bot-skipped`, both containers stop in under a second with exit code 0 and the `shutdown` line. Before the
  fix a malformed token left no line in the log while the stack ran, and showed up as `Application shutdown failed`
  (the Redis and database pools never closed) when `jobs` stopped; a well-formed token that Telegram does not know
  logged `bot-polling-failed` once, but `docker compose stop jobs` then took 10.2 s and ended with exit code 137, because
  aiogram's `start_polling` replaces uvicorn's SIGTERM and SIGINT handlers and leaves ones that do nothing. Now both
  log `bot-polling-failed` at start (`Token is invalid!`, `Telegram server says - Unauthorized`) and `jobs` and `app`
  stop in under a second with exit code 0. A bot that polls successfully needs a real token and was not exercised; a
  unit test checks, with Telegram's answer stubbed, that polling leaves the SIGTERM and SIGINT handlers alone.
- The integrity checker, run the way "Checking a running system" says, on a database with one confirmed booking:
  `docker compose exec -T app python -m scripts.check_invariants` prints `OK: no invariant violations or warnings`
  and exits 0, `--json` prints `{"violations": [], "warnings": []}`, `--self-test` passes. After one night's `sold`
  count was raised by hand, with no booking behind it, it prints `VIOLATION inventory (a): <ids>` and exits 1; it
  leaves no session idle in a transaction.
- HSTS behind a proxy that is trusted: from the host, with `FORWARDED_ALLOW_IPS` set to the Compose gateway, a request
  carrying `X-Forwarded-Proto: https` gets `Strict-Transport-Security: max-age=31536000` and one without it gets none.
- Photo memory and log size, the numbers of "Memory and logs". Before the change to the image, two runs of the stack
  with 39.7 MP uploads: container 140 MiB idle, 623 at the peak of one upload, 861 after 12 uploads, 877 after 30 more
  (6 of them at once), each worker at 441. With `MALLOC_MMAP_THRESHOLD_` set by an override: 150 after the same
  uploads, workers at 86. Built from the edited files: 149 after 12 uploads, 150 after 6 at once, peak 1,091, 1.57 s per
  upload, every photo stored and served as `image/webp`. With a log limit of 10 kB and two files, 700 requests (52 kB)
  left 8.7 kB for `docker logs`.

## Not verified

- A real reverse proxy in front (nginx, Caddy, Traefik): trusted-proxy handling through a real hop, TLS termination,
  HSTS over real HTTPS. Only requests made from the host were exercised (above).
- Several `app` replicas, or `WEB_CONCURRENCY` above 2, under load. `scripts/load_holds.py` refuses a production
  environment, so the hold queue was measured outside the production container.
- Restoring a backup on another physical host; it was restored on a second Compose project of the same Docker host.
- A failed photo write (wrong owner, full disk) is answered as 415 "the file is corrupt", as `add_photo` did before
  this stack; the real error is in the `photo-decode-failed` log line.
- Real payments: the stub and the mock acquirer (`mock_acquiring`) are the only providers; no real acquirer is
  implemented. `HostedPaymentProvider` (`app/modules/payment/provider.py`) is where one goes.
- Mail through a real provider: delivery was exercised against an SMTP server on the same host (aiosmtpd, no TLS)
  and the protocol against a fake one in the test suite. STARTTLS and SSL, a provider's own limits and its answers
  to spam checks, SPF/DKIM/DMARC for the sender's domain and inbox placement were not.

## What startup refuses

`APP_ENV` defaults to `production`, so a deployment that forgets it is checked as one. Unless
`APP_ENV` is `local`, `dev` or `test`, the application stops at boot, listing every problem at once, when:

- `JWT_SECRET` is the placeholder from the repository or shorter than 32 characters (anyone who knows
  the placeholder could sign an admin token);
- `WEBHOOK_SEAL_KEY` (or `WEBHOOK_SEAL_KEY_PREVIOUS`, when set) is not a valid Fernet key;
- `ALLOW_PRIVATE_TARGETS` is on (it lets partner-supplied webhook and iCal URLs reach the internal network);
- `APP_BASE_URL` is not the public `https` address;
- `PAYMENT_MODE` is `fail` or `delayed_stub` (test-only), or has no implementation.

`PAYMENT_MODE=stub` and `PAYMENT_MODE=mock_acquiring` are accepted, since no real provider exists yet, and log a
warning (`payment-stub-outside-dev`) at every start: bookings are confirmed without charging anyone.

## Mail

A booking that is confirmed or cancelled with a refund queues its letters in the same transaction (the partner
always, the guest for a web booking); the `jobs` process sends them every 5 seconds. Details and the failure rules:
`docs/ARCHITECTURE.md` §4.10.

- **Until there is an account**, leave `MAIL_BACKEND=log`. Nothing is sent, the text of every letter stays in the
  admin page "Письма" (`/admin/emails`), and the booking pages say "save the booking code" instead of "we sent you a
  letter". The app logs `email-not-delivered-outside-dev` at every start outside development.
- **Connecting a provider:** set `MAIL_BACKEND=smtp`, `SMTP_HOST`, `SMTP_PORT` (587 with `SMTP_STARTTLS=true`, or 465
  with `SMTP_SSL=true` and `SMTP_STARTTLS=false`), `SMTP_USER`, `SMTP_PASSWORD` and `MAIL_FROM` with an address on a
  domain you control. Publish SPF and DKIM for that domain at the provider's instructions, or the letters land in spam.
  A wrong setting stops the containers at boot with the reason; a wrong account does not (see `email-smtp-auth-failed`).
  A letter the `log` backend already marked as sent is not sent again: only letters still waiting go through the
  new account.
- **Trying it locally without a provider**, with a catcher such as Mailpit: `MAIL_BACKEND=smtp SMTP_HOST=localhost
  SMTP_PORT=1025 SMTP_STARTTLS=false`; the catcher's page shows what a guest would read.
- **What to watch:** admin → "Письма" lists every letter with its state, and "Повторить" puts a failed one back. Log
  lines that need a person: `email-failed-permanently` (a letter that will not go: address refused, attempts
  exhausted; the log line has no addresses, the row in "Письма" has the refused one), `email-smtp-auth-failed` (the account is wrong: the whole queue waits), `email-enqueue-failed` (a
  booking without its letters: the queue did not take the row; the booking itself is fine), `mail-loop-error`.
- **A confirmation that was still waiting for a retry when the booking was cancelled is not sent** (state
  "Пропущено"): the cancellation letter is the news by then.
- **A letter is delivered at least once.** If a process dies after the server accepted a letter and before the queue
  noted it, the letter is sent again under the same `Message-ID`.
- **Personal data:** a letter row holds the recipient's address, and the log backend also the text. Sent rows are
  deleted after 30 days (`MAIL_SENT_RETENTION_DAYS`), failed ones after 180 (`MAIL_FAILED_RETENTION_DAYS`) by the
  `jobs` process. After a real delivery the text is not kept.

## Payments

Until there are acquiring keys, run with `PAYMENT_MODE=mock_acquiring`: the checkout sends the guest to a test
payment page (`/mock-acquirer/pay/{id}`, served by the API, nothing to deploy), the page reports the result to the
API the way a bank does, and the guest comes back to `/checkout/{id}/return`.

- `APP_BASE_URL` must be the address guests open the site on: the payment page and the return address are built
  from it, and the booking token lives in the tab's `sessionStorage`, which a different origin does not share.
- The page offers three outcomes: pay, "bank refused", and "paid but the shop is not told". The last one is
  found by the guest's return (`POST /v1/bookings/{id}/payment/sync`) or, if the guest never comes back, by the
  `payment-reconcile` sweep in the `jobs` process (every 30 seconds, payments older than 60 seconds).
- A payment that arrives after the booking was released (the guest paid on the page after the hold ran out) is
  paid back automatically. `charge-not-returned` (error) is a charge that could not be paid back: refund it by
  hand; the sweep keeps trying. `payment-reconcile-failed` (error) is a sweep that could not reach the provider.
- A real acquirer will call `POST {APP_BASE_URL}/v1/payments/notifications/{provider}`; that path has to be
  reachable from the internet (the notification is authenticated by the provider's signature, not by a login).
- The mock keeps its state in the table `mock_acquirer_payment`; it is empty under any other mode.
- Switching `PAYMENT_MODE` (stub, then the mock, then a real acquirer): a refund goes back through the provider
  that took the money (read from the payment ledger), so bookings paid before the switch stay refundable. Payment
  sessions still open at the moment of the switch are not settled by the new provider: switch when none is open.
