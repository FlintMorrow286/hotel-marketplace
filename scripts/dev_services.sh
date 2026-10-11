#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

fail() {
    printf 'dev_services: %s\n' "$1" >&2
    exit 1
}

read_env_value() {
    local file=$1 name=$2
    [[ -f "$file" ]] || return 0
    python3 - "$file" "$name" <<'PY'
import sys

path, wanted = sys.argv[1:]
with open(path, encoding="utf-8") as env_file:
    for raw in env_file:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != wanted:
            continue
        value = value.strip()
        if value[:1] in ("'", '"') and value.endswith(value[0]):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        sys.stdout.write(value)
        break
PY
}

setting() {
    local name=$1 fallback=$2 value
    value="$(printenv "$name" 2>/dev/null || true)"
    if [[ -n "$value" ]]; then
        printf '%s' "$value"
        return
    fi
    for file in "$ROOT_DIR/.env" "$ROOT_DIR/.env.example"; do
        value="$(read_env_value "$file" "$name")"
        if [[ -n "$value" ]]; then
            printf '%s' "$value"
            return
        fi
    done
    printf '%s' "$fallback"
}

DATABASE_URL="$(setting DATABASE_URL 'postgresql://hotel_user:hotel_pw@localhost:5432/hotel_mp')"
REDIS_URL="$(setting REDIS_URL 'redis://localhost:6379/0')"

DATABASE_PARTS=()
while IFS= read -r -d '' value; do
    DATABASE_PARTS+=("$value")
done < <(DEV_SERVICES_DATABASE_URL="$DATABASE_URL" python3 <<'PY'
import os
import sys
from urllib.parse import unquote, urlsplit

try:
    url = urlsplit(os.environ["DEV_SERVICES_DATABASE_URL"])
    if url.scheme not in ("postgres", "postgresql"):
        raise ValueError
    fields = (
        unquote(url.username or ""),
        unquote(url.password or ""),
        url.hostname or "localhost",
        str(url.port or 5432),
        unquote(url.path.lstrip("/")),
    )
    if not fields[0] or not fields[4]:
        raise ValueError
except ValueError:
    raise SystemExit("DATABASE_URL must be a PostgreSQL URL with a user and database")

for field in fields:
    sys.stdout.buffer.write(field.encode() + b"\0")
PY
)
[[ ${#DATABASE_PARTS[@]} -eq 5 ]] || fail "could not parse DATABASE_URL"
DB_USER=${DATABASE_PARTS[0]}
DB_PASSWORD=${DATABASE_PARTS[1]}
DB_HOST=${DATABASE_PARTS[2]}
DB_PORT=${DATABASE_PARTS[3]}
DB_NAME=${DATABASE_PARTS[4]}
case "$DB_HOST" in
    localhost|127.0.0.1|::1) ;;
    *) fail "DATABASE_URL must point to local PostgreSQL" ;;
esac

REDIS_PARTS=()
while IFS= read -r -d '' value; do
    REDIS_PARTS+=("$value")
done < <(DEV_SERVICES_REDIS_URL="$REDIS_URL" python3 <<'PY'
import os
import sys
from urllib.parse import unquote, urlsplit

try:
    url = urlsplit(os.environ["DEV_SERVICES_REDIS_URL"])
    if url.scheme != "redis":
        raise ValueError
    fields = (
        unquote(url.username or ""),
        unquote(url.password or ""),
        url.hostname or "localhost",
        str(url.port or 6379),
    )
except ValueError:
    raise SystemExit("REDIS_URL must be a redis:// URL")

for field in fields:
    sys.stdout.buffer.write(field.encode() + b"\0")
PY
)
[[ ${#REDIS_PARTS[@]} -eq 4 ]] || fail "could not parse REDIS_URL"
REDIS_USERNAME=${REDIS_PARTS[0]}
REDIS_PASSWORD=${REDIS_PARTS[1]}
REDIS_HOST=${REDIS_PARTS[2]}
REDIS_PORT=${REDIS_PARTS[3]}
case "$REDIS_HOST" in
    localhost|127.0.0.1|::1) ;;
    *) fail "REDIS_URL must point to local Redis" ;;
esac

command -v pg_isready >/dev/null || fail "pg_isready is required (install the PostgreSQL client tools)"
command -v psql >/dev/null || fail "psql is required (install the PostgreSQL client tools)"
command -v redis-cli >/dev/null || fail "redis-cli is required"

postgres_ready() {
    pg_isready -h "$DB_HOST" -p "$DB_PORT" >/dev/null 2>&1
}

run_service_command() {
    if [[ $(id -u) -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null && sudo -n true >/dev/null 2>&1; then
        sudo -n "$@"
    else
        "$@"
    fi
}

pg_data_directory() {
    local candidate
    if [[ -n "${PGDATA:-}" && -s "${PGDATA}/PG_VERSION" ]]; then
        printf '%s' "$PGDATA"
        return
    fi
    for candidate in \
        /var/lib/postgresql/*/main \
        /usr/local/var/postgres \
        /usr/local/var/postgresql@16 \
        /opt/homebrew/var/postgres \
        /opt/homebrew/var/postgresql@16 \
        "$HOME/.local/share/postgres"; do
        if [[ -s "$candidate/PG_VERSION" ]]; then
            printf '%s' "$candidate"
            return
        fi
    done
    if command -v brew >/dev/null; then
        local prefix
        prefix="$(brew --prefix 2>/dev/null || true)"
        for candidate in "$prefix"/var/postgresql@*; do
            if [[ -s "$candidate/PG_VERSION" ]]; then
                printf '%s' "$candidate"
                return
            fi
        done
    fi
}

start_postgres() {
    postgres_ready && return

    if command -v pg_ctlcluster >/dev/null && command -v pg_lsclusters >/dev/null; then
        while read -r version cluster port _; do
            if [[ "$port" == "$DB_PORT" ]]; then
                run_service_command pg_ctlcluster --skip-systemctl-redirect \
                    "$version" "$cluster" start >/dev/null 2>&1 || true
                break
            fi
        done < <(pg_lsclusters --no-header 2>/dev/null || true)
    fi

    if ! postgres_ready && command -v service >/dev/null; then
        run_service_command service postgresql start >/dev/null 2>&1 || true
    fi

    if ! postgres_ready; then
        local pg_ctl pg_data
        pg_ctl="$(command -v pg_ctl 2>/dev/null || true)"
        if [[ -z "$pg_ctl" ]] && command -v pg_config >/dev/null; then
            pg_ctl="$(pg_config --bindir 2>/dev/null)/pg_ctl"
            [[ -x "$pg_ctl" ]] || pg_ctl=
        fi
        pg_data="$(pg_data_directory)"
        if [[ -n "$pg_ctl" && -n "$pg_data" ]]; then
            if [[ $(id -u) -eq 0 ]] && command -v runuser >/dev/null; then
                runuser -u postgres -- "$pg_ctl" -D "$pg_data" start >/dev/null 2>&1 || true
            else
                "$pg_ctl" -D "$pg_data" start >/dev/null 2>&1 || true
            fi
        fi
    fi

    local attempt
    for ((attempt = 0; attempt < 40; attempt++)); do
        postgres_ready && return
        sleep 1
    done
    fail "local PostgreSQL did not become ready"
}

redis_ready() {
    local -a args=(-h "$REDIS_HOST" -p "$REDIS_PORT")
    [[ -z "$REDIS_USERNAME" ]] || args+=(--user "$REDIS_USERNAME")
    if [[ -n "$REDIS_PASSWORD" ]]; then
        REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli "${args[@]}" ping >/dev/null 2>&1
    else
        redis-cli "${args[@]}" ping >/dev/null 2>&1
    fi
}

start_redis() {
    redis_ready && return
    command -v redis-server >/dev/null || fail "redis-server is required to start local Redis"
    if [[ -n "$REDIS_USERNAME" || -n "$REDIS_PASSWORD" ]]; then
        fail "local Redis is down; start it manually with the configured ACL credentials"
    fi
    redis-server --port "$REDIS_PORT" --daemonize yes \
        --pidfile "${TMPDIR:-/tmp}/hoplite-redis-$(id -u)-$REDIS_PORT.pid" \
        --logfile /dev/null >/dev/null 2>&1 || true
    local attempt
    for ((attempt = 0; attempt < 40; attempt++)); do
        redis_ready && return
        sleep 1
    done
    fail "local Redis did not become ready"
}

psql_as_app() {
    local database=$1
    shift
    PGPASSWORD="$DB_PASSWORD" psql -X -q -v ON_ERROR_STOP=1 \
        -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$database" "$@"
}

psql_as_local_admin() {
    if [[ $(id -un) == postgres ]]; then
        psql -X -q -v ON_ERROR_STOP=1 -p "$DB_PORT" -d postgres "$@"
    elif [[ $(id -u) -eq 0 ]] && command -v runuser >/dev/null; then
        runuser --preserve-environment -u postgres -- \
            psql -X -q -v ON_ERROR_STOP=1 -p "$DB_PORT" -d postgres "$@"
    elif command -v sudo >/dev/null && sudo -n -u postgres true >/dev/null 2>&1; then
        sudo -n --preserve-env=DEV_SERVICES_ROLE,DEV_SERVICES_PASSWORD,DEV_SERVICES_DATABASE \
            -u postgres psql -X -q -v ON_ERROR_STOP=1 -p "$DB_PORT" -d postgres "$@"
    else
        return 127
    fi
}

PSQL_ADMIN_KIND=
psql_as_admin() {
    if [[ "$PSQL_ADMIN_KIND" == app ]]; then
        psql_as_app postgres "$@"
    else
        psql_as_local_admin "$@"
    fi
}

ensure_role_and_database() {
    export DEV_SERVICES_ROLE="$DB_USER"
    export DEV_SERVICES_PASSWORD="$DB_PASSWORD"
    export DEV_SERVICES_DATABASE="$DB_NAME"

    psql_as_admin <<'SQL' >/dev/null
\getenv target_role DEV_SERVICES_ROLE
\getenv target_password DEV_SERVICES_PASSWORD
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L CREATEDB', :'target_role', :'target_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'target_role');
\gexec
SQL

    psql_as_admin <<'SQL' >/dev/null
\getenv target_role DEV_SERVICES_ROLE
\getenv target_database DEV_SERVICES_DATABASE
SELECT format('CREATE DATABASE %I OWNER %I', :'target_database', :'target_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'target_database');
\gexec
SQL
}

wait_for_postgres() {
    local attempt
    for ((attempt = 0; attempt < 40; attempt++)); do
        postgres_ready && return
        sleep 1
    done
    fail "local PostgreSQL did not become ready"
}

wait_for_redis() {
    local attempt
    for ((attempt = 0; attempt < 40; attempt++)); do
        redis_ready && return
        sleep 1
    done
    fail "local Redis did not become ready"
}

start_postgres
wait_for_postgres
start_redis
wait_for_redis

if ! psql_as_app "$DB_NAME" -c 'SELECT 1' >/dev/null 2>&1; then
    if psql_as_app postgres -c 'SELECT 1' >/dev/null 2>&1; then
        PSQL_ADMIN_KIND=app
        if ! ensure_role_and_database; then
            PSQL_ADMIN_KIND=
        fi
    fi
    if [[ -z "$PSQL_ADMIN_KIND" ]]; then
        if psql_as_local_admin -c 'SELECT 1' >/dev/null 2>&1; then
            PSQL_ADMIN_KIND=local
            ensure_role_and_database || fail "could not provision the configured database role and database"
        else
            fail "cannot provision the configured database; grant local PostgreSQL admin access"
        fi
    fi
    psql_as_app "$DB_NAME" -c 'SELECT 1' >/dev/null 2>&1 \
        || fail "database is ready but DATABASE_URL credentials cannot connect"
fi

printf 'PostgreSQL role/database ready; Redis ready.\n'
